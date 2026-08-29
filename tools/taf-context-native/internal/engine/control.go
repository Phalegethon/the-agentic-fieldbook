package engine

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"sort"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/extract"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const engineVersion = "0.1.0"

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
	warnings := append([]string(nil), inventoryResult.Warnings...)
	records := make([]model.Record, 0)
	parserIDs := engine.dependencies.ParserIDs()
	for _, item := range inventoryResult.Paths {
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		maximum := int64(productionLimits().MaximumSourceFileBytes)
		if item.Language == "markdown" {
			maximum = int64(productionLimits().MaximumMarkdownFileBytes)
		}
		file, openErr := engine.dependencies.OpenFile(roots, item.RelativePath, maximum)
		if openErr != nil || file.RelativePath != item.RelativePath || file.Size != item.Size || file.SHA256 != item.SHA256 {
			return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
		}
		fileRecords, report := engine.dependencies.Extract(ctx, file)
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if expected, ok := parserIDs[item.Language]; !ok || report.ParserVersion != expected {
			return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
		}
		coverage.ParseFailureCount += report.ParseFailures
		warnings = append(warnings, report.WarningCodes...)
		records = append(records, fileRecords...)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Identity < records[j].Identity })
	manifest := model.Manifest{
		FormatVersion: "1", EngineVersion: engineVersion,
		Binding:                 model.Binding{RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity, CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint},
		InclusionPolicyIdentity: currentInclusionPolicyIdentity(), ExclusionPolicyIdentity: currentExclusionPolicyIdentity(),
		ParserIdentities: cloneStrings(parserIDs), Coverage: coverage,
		SourceBindingDigest: sourceBinding(inventoryResult.Paths), SemanticDigest: semanticBinding(records),
	}
	snapshot, buildErr := engine.dependencies.Build(roots, manifest, records)
	if buildErr != nil {
		return engine.result(request, wire.Error, "unknown", nil, coverage, "rebuild-index"), nil
	}
	status, freshness, action := wire.Ready, "exact", "use-index"
	if inventoryResult.Partial || coverage.ParseFailureCount != 0 {
		status, freshness, action = wire.Partial, "partial", "rebuild-index"
	}
	result := engine.result(request, status, freshness, ptr(snapshot.IndexIdentity), coverage, action)
	result.ParserVersions = cloneStrings(parserIDs)
	result.Warnings = warnings
	return result, nil
}

func (engine *Engine) state(ctx context.Context, roots *boundary.Roots, request wire.Request, metrics bool) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	status, err := engine.dependencies.Inspect(roots)
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
		Status: status, ProviderIdentity: "taf.native.level1", ProviderVersion: engineVersion, IndexIdentity: index,
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
	if manifest.Coverage.ParseFailureCount != 0 || manifest.Coverage.PathCoverage < 1 || manifest.Coverage.LanguageCoverage < 1 {
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
	parts := []string{"taf-level1-semantic-binding-v1"}
	for _, record := range records {
		parts = append(parts, record.Identity, record.Path, string(record.RecordKind), record.QualifiedName, record.SourceDigest, record.ExtractionMethod, string(record.EvidenceClass))
	}
	return hashParts(parts)
}

func currentInclusionPolicyIdentity() string {
	inclusion, _ := inventory.PolicyIdentities()
	return inclusion
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
