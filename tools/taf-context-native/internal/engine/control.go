package engine

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"runtime"
	"sort"
	"sync"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/extract"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const engineVersion = "0.1.0"

const maximumAggregateRecordBytes = 64 << 20

const maximumExtractionWorkers = 8

type extractedPath struct {
	records []model.Record
	report  extract.Report
	invalid bool
}

func (engine *Engine) estimate(ctx context.Context, roots *boundary.Roots, request wire.Request) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	inventoryResult, err := engine.dependencies.Collect(*roots, inventory.ModeEstimate)
	if err != nil {
		return wire.Result{}, err
	}
	result := engine.result(request, wire.Partial, "partial", nil, inventoryResult.Coverage, "build-index")
	result.Warnings = append(result.Warnings, inventoryResult.Warnings...)
	return result, nil
}

func (engine *Engine) build(ctx context.Context, roots *boundary.Roots, request wire.Request) (wire.Result, error) {
	inventoryResult, err := engine.dependencies.Collect(*roots, inventory.ModeBuild)
	if err != nil {
		return wire.Result{}, err
	}
	coverage := cloneCoverage(inventoryResult.Coverage)
	if inventoryResult.Partial {
		coverage.ExclusionReasonCounts["incomplete-inventory"]++
	}
	warnings := append([]string(nil), inventoryResult.Warnings...)
	extractionWarnings := make(map[string][]string)
	records := make([]model.Record, 0)
	aggregateBytes := 0
	parserIDs := engine.dependencies.ParserIDs()
	extracted, extractErr := engine.extractPaths(ctx, roots, inventoryResult.Paths)
	if extractErr != nil {
		return wire.Result{}, extractErr
	}
	for index, item := range inventoryResult.Paths {
		fileRecords, report := extracted[index].records, extracted[index].report
		if extracted[index].invalid {
			return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
		}
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if expected, ok := parserIDs[item.Language]; !ok || report.ParserVersion != expected {
			return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
		}
		coverage.ParseFailureCount += report.ParseFailures
		if report.Incomplete() && report.ParseFailures == 0 {
			coverage.ParseFailureCount++
			coverage.ExclusionReasonCounts["incomplete-extraction"]++
		}
		warnings = appendBoundedWarnings(warnings, report.WarningCodes...)
		if codes := appendBoundedWarnings(nil, report.WarningCodes...); len(codes) != 0 {
			extractionWarnings[item.RelativePath] = codes
		}
		for _, record := range fileRecords {
			cost := recordFootprint(record)
			if cost > maximumAggregateRecordBytes-aggregateBytes {
				coverage.ParseFailureCount++
				result := engine.result(request, wire.Partial, "partial", nil, coverage, "rebuild-index")
				result.Warnings = appendBoundedWarnings(warnings, "engine-aggregate-limit")
				return result, nil
			}
			aggregateBytes += cost
		}
		records = append(records, fileRecords...)
	}
	if hasBoundedWarning(warnings, "warning-limit") {
		coverage.ExclusionReasonCounts["warning-limit"]++
		coverage.ParseFailureCount++
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Identity < records[j].Identity })
	for index := 1; index < len(records); index++ {
		if records[index-1].Identity == records[index].Identity {
			return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
		}
	}
	manifest := model.Manifest{
		FormatVersion: "2", EngineVersion: engineVersion,
		Binding:                 model.Binding{RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity, CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint},
		InclusionPolicyIdentity: currentInclusionPolicyIdentity(), ExclusionPolicyIdentity: currentExclusionPolicyIdentity(),
		ParserIdentities: cloneStrings(parserIDs), Coverage: coverage,
		SourceBindingDigest: sourceBinding(inventoryResult.Paths), SemanticDigest: semanticBinding(records),
		SourceCatalog: sourceCatalog(inventoryResult, extractionWarnings),
	}
	snapshot, buildErr := engine.dependencies.Build(ctx, roots, manifest, records)
	if buildErr != nil {
		if errors.Is(buildErr, context.Canceled) || errors.Is(buildErr, context.DeadlineExceeded) {
			return wire.Result{}, buildErr
		}
		return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
	}
	engine.rememberSnapshot(snapshot)
	status, freshness, action := wire.Ready, "exact", "use-index"
	if !completeCoverage(coverage, false) {
		status, freshness, action = wire.Partial, "partial", "rebuild-index"
	}
	result := engine.result(request, status, freshness, ptr(snapshot.IndexIdentity), coverage, action)
	result.ParserVersions = cloneStrings(parserIDs)
	result.Warnings = warnings
	return result, nil
}

func (engine *Engine) extractPaths(ctx context.Context, roots *boundary.Roots, paths []inventory.Path) ([]extractedPath, error) {
	results := make([]extractedPath, len(paths))
	if len(paths) == 0 {
		return results, ctx.Err()
	}
	workers := min(len(paths), maximumExtractionWorkers, max(1, runtime.GOMAXPROCS(0)))
	jobs := make(chan int, len(paths))
	for index := range paths {
		jobs <- index
	}
	close(jobs)
	var wait sync.WaitGroup
	wait.Add(workers)
	for range workers {
		go func() {
			defer wait.Done()
			for index := range jobs {
				if ctx.Err() != nil {
					continue
				}
				item := paths[index]
				maximum := int64(productionLimits().MaximumSourceFileBytes)
				if item.Language == "markdown" {
					maximum = int64(productionLimits().MaximumMarkdownFileBytes)
				}
				file := boundary.StableFile{RelativePath: item.RelativePath, Bytes: item.Bytes, SHA256: item.SHA256, Size: item.Size}
				var err error
				if !item.BodyRetained {
					file, err = engine.dependencies.OpenFile(roots, item.RelativePath, maximum)
				}
				if err != nil || file.RelativePath != item.RelativePath || file.Size != item.Size || file.SHA256 != item.SHA256 || int64(len(file.Bytes)) != item.Size {
					results[index].invalid = true
					continue
				}
				results[index].records, results[index].report = engine.dependencies.Extract(ctx, file)
			}
		}()
	}
	wait.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return results, nil
}

func sourceCatalog(result inventory.Result, extractionWarnings map[string][]string) model.SourceCatalog {
	catalog := model.SourceCatalog{Partial: result.Partial, Warnings: append([]string(nil), result.Warnings...)}
	for _, item := range result.Paths {
		catalog.Paths = append(catalog.Paths, model.SourcePath{RelativePath: item.RelativePath, Language: item.Language, Size: item.Size, SHA256: item.SHA256})
	}
	for _, item := range result.Exclusions {
		catalog.Exclusions = append(catalog.Exclusions, model.SourceExclusion{RelativePath: item.RelativePath, Reason: item.Reason})
	}
	for relative, codes := range extractionWarnings {
		catalog.ExtractionWarnings = append(catalog.ExtractionWarnings, model.SourceWarning{RelativePath: relative, Codes: append([]string(nil), codes...)})
	}
	sort.Slice(catalog.ExtractionWarnings, func(i, j int) bool {
		return catalog.ExtractionWarnings[i].RelativePath < catalog.ExtractionWarnings[j].RelativePath
	})
	return catalog
}

func (engine *Engine) state(ctx context.Context, roots *boundary.Roots, request wire.Request, metrics bool) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	status, err := engine.dependencies.Inspect(ctx, roots)
	if err != nil {
		action := "rebuild-index"
		if errors.Is(err, store.ErrNoCurrent) {
			action = "build-index"
		}
		return engine.result(request, wire.Error, "unusable", request.IndexIdentity, emptyCoverage(), action), nil
	}
	freshness, action := freshnessFor(request, status.Manifest, status.IndexIdentity, engine.dependencies.ParserIDs())
	resultStatus := wire.Ready
	if freshness != "exact" {
		resultStatus = wire.Stale
	}
	if freshness == "partial" {
		resultStatus = wire.Partial
	}
	result := engine.result(request, resultStatus, freshness, request.IndexIdentity, status.Manifest.Coverage, action)
	result.ParserVersions = cloneStrings(status.Manifest.ParserIdentities)
	if metrics && resultStatus != wire.Ready {
		result.Warnings = []string{"metrics-stale"}
	}
	return result, nil
}

func (engine *Engine) unsupported(request wire.Request) wire.Result {
	return engine.result(request, wire.Unsupported, "unknown", request.IndexIdentity, emptyCoverage(), "rebuild-index")
}

func (engine *Engine) result(request wire.Request, status wire.Status, freshness string, index *string, coverage model.Coverage, action string) wire.Result {
	if index == nil && request.IndexIdentity != nil && request.Operation != wire.Estimate && request.Operation != wire.Build {
		index = request.IndexIdentity
	}
	return wire.Result{
		SchemaVersion: "1", RequestIdentity: request.RequestIdentity, Operation: request.Operation,
		Status: status, ProviderIdentity: "taf-context", ProviderVersion: engineVersion, IndexIdentity: index,
		RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity, CommittedHead: request.CommittedHead,
		DirtyOverlayFingerprint: request.DirtyOverlayFingerprint, Freshness: freshness,
		ParserVersions: cloneStrings(engine.dependencies.ParserIDs()), Coverage: wireCoverage(coverage), Findings: []wire.Finding{},
		Warnings: []string{}, NextSafeAction: action,
	}
}

func freshnessFor(request wire.Request, manifest model.Manifest, index string, parserIDs map[string]string) (string, string) {
	if request.IndexIdentity == nil || *request.IndexIdentity != index || manifest.InclusionPolicyIdentity != currentInclusionPolicyIdentity() || manifest.ExclusionPolicyIdentity != currentExclusionPolicyIdentity() || !sameStrings(manifest.ParserIdentities, parserIDs) || manifest.Binding.RepositoryIdentity != request.RepositoryIdentity || manifest.Binding.WorktreeIdentity != request.WorktreeIdentity {
		return "structurally-stale", "rebuild-index"
	}
	if manifest.Binding.CommittedHead != request.CommittedHead {
		return "incrementally-stale", "rebuild-index"
	}
	if manifest.Binding.DirtyOverlayFingerprint != request.DirtyOverlayFingerprint {
		return "commit-fresh-worktree-stale", "rebuild-index"
	}
	if !completeCoverage(manifest.Coverage, false) {
		return "partial", "rebuild-index"
	}
	return "exact", "use-index"
}

func sourceBinding(paths []inventory.Path) string {
	parts := []string{"taf-level1-source-binding-v1"}
	for _, item := range paths {
		parts = append(parts, item.RelativePath, item.SHA256)
	}
	return hashParts(parts)
}

func semanticBinding(records []model.Record) string {
	parts := []string{"taf-level1-semantic-binding-v2"}
	for _, record := range records {
		parts = append(parts, record.Identity, record.Path, fmt.Sprintf("%d", record.StartLine), fmt.Sprintf("%d", record.EndLine), record.Language, string(record.RecordKind), record.SourceType, record.QualifiedName, record.ExtractionMethod, string(record.EvidenceClass), fmt.Sprintf("%d", len(record.SearchTerms)))
		parts = append(parts, record.SearchTerms...)
		parts = append(parts, record.SourceDigest, record.Preview)
	}
	return hashParts(parts)
}

func completeCoverage(coverage model.Coverage, inventoryPartial bool) bool {
	if inventoryPartial || coverage.IndexedPathCount == 0 || coverage.ParseFailureCount != 0 {
		return false
	}
	for _, marker := range []string{"incomplete-inventory", "incomplete-extraction", "engine-aggregate-limit", "warning-limit"} {
		if coverage.ExclusionReasonCounts[marker] != 0 {
			return false
		}
	}
	return true
}

func recordFootprint(record model.Record) int {
	// Conservative retained Go headers plus every payload held across the
	// engine/store handoff; this leaves Task 6's store headroom intact.
	size := 256 + len(record.Identity) + len(record.Path) + len(record.Language) + len(record.SourceType) + len(record.QualifiedName) + len(record.ExtractionMethod) + len(record.SourceDigest) + len(record.Preview)
	for _, term := range record.SearchTerms {
		size += 32 + len(term)
	}
	return size
}

func appendBoundedWarnings(current []string, added ...string) []string {
	seen := make(map[string]struct{}, len(current)+len(added))
	overflow := false
	for _, warning := range append(append([]string(nil), current...), added...) {
		if warning == "warning-limit" {
			overflow = true
			continue
		}
		seen[warning] = struct{}{}
	}
	output := make([]string, 0, len(seen))
	for warning := range seen {
		output = append(output, warning)
	}
	sort.Strings(output)
	if len(output) > 63 {
		overflow = true
		output = output[:63]
	}
	if !overflow {
		return output
	}
	output = append(output, "warning-limit")
	sort.Strings(output)
	return output
}

func hasBoundedWarning(warnings []string, want string) bool {
	for _, warning := range warnings {
		if warning == want {
			return true
		}
	}
	return false
}

func currentInclusionPolicyIdentity() string {
	inclusion, _ := inventory.PolicyIdentities()
	return hashParts([]string{"taf-level1-inclusion-composite-v1", inclusion, extract.PolicyDescriptor()})
}
func currentExclusionPolicyIdentity() string {
	_, exclusion := inventory.PolicyIdentities()
	return exclusion
}

func hashParts(parts []string) string {
	hash := sha256.New()
	var length [8]byte
	for _, part := range parts {
		binary.BigEndian.PutUint64(length[:], uint64(len(part)))
		_, _ = hash.Write(length[:])
		_, _ = hash.Write([]byte(part))
	}
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func cloneStrings(input map[string]string) map[string]string {
	output := make(map[string]string, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}
func sameStrings(left, right map[string]string) bool {
	if len(left) != len(right) {
		return false
	}
	for key, value := range left {
		if right[key] != value {
			return false
		}
	}
	return true
}
func cloneCoverage(input model.Coverage) model.Coverage {
	input.ExclusionReasonCounts = appendCounts(input.ExclusionReasonCounts)
	return input
}
func appendCounts(input map[string]int) map[string]int {
	output := make(map[string]int, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}
func emptyCoverage() model.Coverage { return model.Coverage{ExclusionReasonCounts: map[string]int{}} }
func wireCoverage(input model.Coverage) wire.Coverage {
	return wire.Coverage{PathCoverage: input.PathCoverage, LanguageCoverage: input.LanguageCoverage, IndexedPathCount: input.IndexedPathCount, ExcludedPathCount: input.ExcludedPathCount, UnsupportedLanguageCount: input.UnsupportedLanguageCount, ParseFailureCount: input.ParseFailureCount, ExclusionReasonCounts: appendCounts(input.ExclusionReasonCounts)}
}
func ptr(value string) *string { return &value }

var _ extract.Report
