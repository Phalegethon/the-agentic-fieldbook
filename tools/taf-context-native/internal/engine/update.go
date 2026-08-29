package engine

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path"
	"regexp"
	"sort"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// update is deliberately a replacement operation: it first proves the exact
// prior generation and the requested after-binding, then opens only paths the
// Level 0 document names. It never turns an uncertain delta into a full build.
func (engine *Engine) update(ctx context.Context, roots *boundary.Roots, request wire.Request, documentPath *string) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	if documentPath == nil || !safeUpdatePath(*documentPath) {
		return engine.staleUpdate(request, emptyCoverage()), nil
	}

	// The control document is a separate, bounded input. Its own lexical path
	// is validated before its capability read; no declared source path is opened
	// until all document/binding checks below have succeeded.
	file, err := engine.dependencies.OpenControl(roots, *documentPath, int64(productionLimits().MaximumWireBytes))
	if err != nil || file.RelativePath != *documentPath {
		return engine.staleUpdate(request, emptyCoverage()), nil
	}
	document, ok := decodeChangeDocument(file.Bytes)
	if !ok {
		return engine.staleUpdate(request, emptyCoverage()), nil
	}
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}

	if request.IndexIdentity == nil {
		return engine.staleUpdate(request, emptyCoverage()), nil
	}
	snapshot, cached := engine.cachedIndex(*request.IndexIdentity)
	status := store.Status{}
	if cached {
		current, currentErr := engine.dependencies.CurrentGeneration(ctx, roots)
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if currentErr != nil || current != snapshot.Manifest.GenerationIdentity {
			return engine.staleUpdate(request, snapshot.Manifest.Coverage), nil
		}
		status = store.Status{Ready: true, Manifest: snapshot.Manifest, IndexIdentity: snapshot.IndexIdentity, GenerationIdentity: snapshot.Manifest.GenerationIdentity, InstalledBytes: snapshot.InstalledBytes}
	} else {
		var inspectErr error
		status, inspectErr = engine.dependencies.Inspect(ctx, roots)
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if inspectErr != nil {
			return engine.staleUpdate(request, emptyCoverage()), nil
		}
		var loadErr error
		snapshot, loadErr = engine.dependencies.Load(ctx, roots, *request.IndexIdentity)
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if loadErr != nil {
			return engine.staleUpdate(request, status.Manifest.Coverage), nil
		}
	}
	if snapshot.IndexIdentity != status.IndexIdentity || snapshot.Manifest.GenerationIdentity != status.GenerationIdentity {
		return engine.staleUpdate(request, status.Manifest.Coverage), nil
	}
	if !validUpdateBindings(document, snapshot.Manifest, snapshot.IndexIdentity, request) {
		return engine.staleUpdate(request, snapshot.Manifest.Coverage), nil
	}
	if snapshot.Manifest.InclusionPolicyIdentity != currentInclusionPolicyIdentity() || snapshot.Manifest.ExclusionPolicyIdentity != currentExclusionPolicyIdentity() || !sameStrings(snapshot.Manifest.ParserIdentities, engine.dependencies.ParserIDs()) {
		return engine.staleUpdate(request, snapshot.Manifest.Coverage), nil
	}

	counters := model.WorkCounters{ChangedPaths: len(document.ChangedPaths)}
	defer func() { engine.observeUpdateCounters(counters) }()
	changed := make(map[string]struct{}, len(document.ChangedPaths))
	for _, changedPath := range document.ChangedPaths {
		changed[changedPath] = struct{}{}
	}
	records := make([]model.Record, 0, len(snapshot.Records))
	for _, record := range snapshot.Records {
		if _, replaced := changed[record.Path]; !replaced {
			records = append(records, record)
		}
	}
	catalog := cloneSourceCatalog(snapshot.Manifest.SourceCatalog)
	if len(catalog.Paths) == 0 && snapshot.Manifest.Coverage.IndexedPathCount != 0 {
		return engine.staleUpdate(request, snapshot.Manifest.Coverage), nil
	}
	paths := make(map[string]model.SourcePath, len(catalog.Paths))
	for _, item := range catalog.Paths {
		paths[item.RelativePath] = item
	}
	exclusions := make(map[string]model.SourceExclusion, len(catalog.Exclusions))
	for _, item := range catalog.Exclusions {
		exclusions[item.RelativePath] = item
	}
	extractionWarnings := make(map[string][]string, len(catalog.ExtractionWarnings))
	for _, item := range catalog.ExtractionWarnings {
		extractionWarnings[item.RelativePath] = append([]string(nil), item.Codes...)
	}
	for _, changedPath := range document.ChangedPaths {
		delete(paths, changedPath)
		delete(extractionWarnings, changedPath)
		// Build stores directory exclusions at their canonical ancestor. A delta
		// naming vendor/file.go must therefore retire both a leaf exclusion and
		// the vendor catalog entry before it decides whether that ancestor still
		// exists. Retaining it makes a nested deletion diverge from a clean build.
		for excludedPath := range exclusions {
			if catalogPathsOverlap(excludedPath, changedPath) {
				delete(exclusions, excludedPath)
			}
		}
	}
	coverage := cloneCoverage(snapshot.Manifest.Coverage)
	warnings := append([]string(nil), catalog.Warnings...)
	for _, codes := range extractionWarnings {
		warnings = appendBoundedWarnings(warnings, codes...)
	}
	parserIDs := engine.dependencies.ParserIDs()
	witnesses := make(map[string]updateWitness, len(document.ChangedPaths))
	for _, changedPath := range document.ChangedPaths {
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		classification, classifyErr := inventory.ClassifyDeclared(roots, changedPath)
		if classifyErr != nil {
			return engine.updateFailure(request, coverage), nil
		}
		maximum := sourceMaximum(changedPath)
		witness, changedFile, witnessErr := engine.captureUpdateWitness(roots, changedPath, maximum, classification, &counters)
		if witnessErr != nil {
			return engine.updateFailure(request, coverage), nil
		}
		witnesses[changedPath] = witness
		if classification.Exclusion != "" {
			exclusions[classification.ExclusionPath] = model.SourceExclusion{RelativePath: classification.ExclusionPath, Reason: classification.Exclusion}
		}
		if witness.missing {
			continue
		}
		if classification.Exclusion != "" {
			continue
		}
		if witness.oversized {
			exclusions[changedPath] = model.SourceExclusion{RelativePath: changedPath, Reason: inventory.ExcludedOversized}
			continue
		}
		fileRecords, report := engine.dependencies.Extract(ctx, changedFile)
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if inventory.IsBinary(changedFile.Bytes) {
			exclusions[changedPath] = model.SourceExclusion{RelativePath: changedPath, Reason: inventory.ExcludedBinary}
			continue
		}
		if fileRecords == nil && report.ParserVersion == "" {
			return engine.updateFailure(request, coverage), nil
		}
		if expected := parserIDs[classification.Language]; expected == "" || report.ParserVersion != expected || report.ParseFailures != 0 || report.Incomplete() {
			return engine.updateFailure(request, coverage), nil
		}
		counters.ParsedRepositoryFiles++
		if codes := appendBoundedWarnings(nil, report.WarningCodes...); len(codes) != 0 {
			extractionWarnings[changedPath] = codes
		}
		warnings = appendBoundedWarnings(warnings, report.WarningCodes...)
		paths[changedPath] = model.SourcePath{RelativePath: changedPath, Language: classification.Language, Size: changedFile.Size, SHA256: changedFile.SHA256}
		records = append(records, fileRecords...)
	}
	// Publication is legal only while every declared path still has the exact
	// classification and stable read witness observed above. This second pass
	// is deliberately declaration-local: it can reopen only named paths and
	// catches create-after-missing, delete, replacement, and content races.
	for _, changedPath := range document.ChangedPaths {
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		previous := witnesses[changedPath]
		classification, classifyErr := inventory.ClassifyDeclared(roots, changedPath)
		if classifyErr != nil || !classification.Same(previous.classification) {
			return engine.updateFailure(request, coverage), nil
		}
		current, _, witnessErr := engine.captureUpdateWitness(roots, changedPath, sourceMaximum(changedPath), classification, &counters)
		if witnessErr != nil || !current.same(previous) {
			return engine.updateFailure(request, coverage), nil
		}
	}
	catalog.Paths = sortedSourcePaths(paths)
	catalog.Exclusions = sortedSourceExclusions(exclusions)
	catalog.ExtractionWarnings = sortedSourceWarnings(extractionWarnings)
	coverage = coverageForCatalog(catalog)
	sort.Slice(records, func(i, j int) bool { return records[i].Identity < records[j].Identity })
	for i := 1; i < len(records); i++ {
		if records[i-1].Identity == records[i].Identity {
			return engine.updateFailure(request, coverage), nil
		}
	}
	manifest := snapshot.Manifest
	manifest.Binding = model.Binding{RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity, CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint}
	manifest.Coverage = coverage
	manifest.SourceBindingDigest = sourceBinding(sourceCatalogPaths(catalog))
	manifest.SemanticDigest = semanticBinding(records)
	manifest.SourceCatalog = catalog
	publicationBarrier := func() error {
		if cached {
			current, currentErr := engine.dependencies.CurrentGeneration(ctx, roots)
			if currentErr != nil || current != snapshot.Manifest.GenerationIdentity {
				return errors.New("selected generation changed before publication")
			}
		}
		for _, changedPath := range document.ChangedPaths {
			previous := witnesses[changedPath]
			classification, classifyErr := inventory.ClassifyDeclared(roots, changedPath)
			if classifyErr != nil || !classification.Same(previous.classification) {
				return errors.New("declared classification changed before publication")
			}
			current, _, witnessErr := engine.captureUpdateWitness(roots, changedPath, sourceMaximum(changedPath), classification, &counters)
			if witnessErr != nil || !current.same(previous) {
				return errors.New("declared path changed before publication")
			}
		}
		return nil
	}
	var published store.Snapshot
	var buildErr error
	if cached {
		published, buildErr = engine.dependencies.BuildCachedWithBarrier(ctx, roots, snapshot, manifest, records, publicationBarrier)
	} else {
		published, buildErr = engine.dependencies.BuildWithBarrier(ctx, roots, manifest, records, publicationBarrier)
	}
	if buildErr != nil {
		return engine.updateFailure(request, coverage), nil
	}
	engine.rememberSnapshot(published)
	result := engine.result(request, wire.Ready, "exact", ptr(published.IndexIdentity), coverage, "use-index")
	result.ParserVersions = cloneStrings(parserIDs)
	result.Warnings = warnings
	return result, nil
}

type updateWitness struct {
	classification inventory.DeclaredClassification
	missing        bool
	oversized      bool
	size           int64
	digest         string
	identity       boundary.FileIdentity
}

func (witness updateWitness) same(other updateWitness) bool {
	if !witness.classification.Same(other.classification) || witness.missing != other.missing || witness.oversized != other.oversized || witness.size != other.size || witness.digest != other.digest {
		return false
	}
	if witness.identity.Valid() || other.identity.Valid() {
		return witness.identity.Same(other.identity)
	}
	return true
}

func (engine *Engine) captureUpdateWitness(roots *boundary.Roots, relative string, maximum int64, classification inventory.DeclaredClassification, counters *model.WorkCounters) (updateWitness, boundary.StableFile, error) {
	witness := updateWitness{classification: classification}
	beforeIO := roots.IOObservation()
	file, err := engine.dependencies.OpenFile(roots, relative, maximum)
	afterIO := roots.IOObservation()
	if counters != nil {
		// Production locality accounting is derived from the retained-root I/O
		// boundary, not from a successful return value. This keeps failure and
		// retry paths honest and cannot be inflated by a helper-only increment.
		counters.OpenedRepositoryFiles += afterIO.FullBodyOpens - beforeIO.FullBodyOpens
		counters.ReadRepositoryBytes += int64(afterIO.FullBodyBytes - beforeIO.FullBodyBytes)
	}
	if errors.Is(err, boundary.ErrRepositoryPathNotFound) {
		witness.missing = true
		return witness, boundary.StableFile{}, nil
	}
	if errors.Is(err, boundary.ErrFileTooLarge) {
		if file.RelativePath != relative || !file.Identity.Valid() || len(file.Bytes) != int(maximum)+1 {
			return updateWitness{}, boundary.StableFile{}, errors.New("oversized declared path lacks bounded stable witness")
		}
		witness.oversized = true
		witness.size, witness.digest, witness.identity = file.Size, file.SHA256, file.Identity
		return witness, boundary.StableFile{}, nil
	}
	if err != nil || file.RelativePath != relative {
		return updateWitness{}, boundary.StableFile{}, errors.New("declared path could not be stably opened")
	}
	witness.size, witness.digest, witness.identity = file.Size, file.SHA256, file.Identity
	if !witness.identity.Valid() {
		return updateWitness{}, boundary.StableFile{}, errors.New("declared path lacks stable identity")
	}
	return witness, file, nil
}

func (engine *Engine) staleUpdate(request wire.Request, coverage model.Coverage) wire.Result {
	return engine.result(request, wire.Stale, "structurally-stale", request.IndexIdentity, coverage, "rebuild-index")
}

func (engine *Engine) updateFailure(request wire.Request, coverage model.Coverage) wire.Result {
	return engine.result(request, wire.Error, "unknown", request.IndexIdentity, coverage, "rebuild-index")
}

func safeUpdatePath(value string) bool {
	if value == "" || strings.ContainsAny(value, "\\\\\x00") || path.IsAbs(value) || path.Clean(value) != value {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}

func sourceMaximum(relative string) int64 {
	if strings.HasSuffix(strings.ToLower(relative), ".md") || strings.HasSuffix(strings.ToLower(relative), ".mdx") {
		return int64(productionLimits().MaximumMarkdownFileBytes)
	}
	return int64(productionLimits().MaximumSourceFileBytes)
}

func cloneSourceCatalog(catalog model.SourceCatalog) model.SourceCatalog {
	catalog.Paths = append([]model.SourcePath(nil), catalog.Paths...)
	catalog.Exclusions = append([]model.SourceExclusion(nil), catalog.Exclusions...)
	catalog.Warnings = append([]string(nil), catalog.Warnings...)
	catalog.ExtractionWarnings = append([]model.SourceWarning(nil), catalog.ExtractionWarnings...)
	for index := range catalog.ExtractionWarnings {
		catalog.ExtractionWarnings[index].Codes = append([]string(nil), catalog.ExtractionWarnings[index].Codes...)
	}
	return catalog
}

func sortedSourceWarnings(values map[string][]string) []model.SourceWarning {
	warnings := make([]model.SourceWarning, 0, len(values))
	for relative, codes := range values {
		warnings = append(warnings, model.SourceWarning{RelativePath: relative, Codes: append([]string(nil), codes...)})
	}
	sort.Slice(warnings, func(i, j int) bool { return warnings[i].RelativePath < warnings[j].RelativePath })
	return warnings
}

func sortedSourcePaths(values map[string]model.SourcePath) []model.SourcePath {
	paths := make([]model.SourcePath, 0, len(values))
	for _, item := range values {
		paths = append(paths, item)
	}
	sort.Slice(paths, func(i, j int) bool { return paths[i].RelativePath < paths[j].RelativePath })
	return paths
}

func sortedSourceExclusions(values map[string]model.SourceExclusion) []model.SourceExclusion {
	exclusions := make([]model.SourceExclusion, 0, len(values))
	for _, item := range values {
		exclusions = append(exclusions, item)
	}
	sort.Slice(exclusions, func(i, j int) bool { return exclusions[i].RelativePath < exclusions[j].RelativePath })
	return exclusions
}

func catalogPathsOverlap(left, right string) bool {
	return left == right || strings.HasPrefix(left, right+"/") || strings.HasPrefix(right, left+"/")
}

func sourceCatalogPaths(catalog model.SourceCatalog) []inventory.Path {
	paths := make([]inventory.Path, 0, len(catalog.Paths))
	for _, item := range catalog.Paths {
		paths = append(paths, inventory.Path{RelativePath: item.RelativePath, Language: item.Language, Size: item.Size, SHA256: item.SHA256})
	}
	return paths
}

func coverageForCatalog(catalog model.SourceCatalog) model.Coverage {
	coverage := model.Coverage{IndexedPathCount: len(catalog.Paths), ExclusionReasonCounts: map[string]int{}}
	regularExcluded, supported := 0, len(catalog.Paths)
	for _, item := range catalog.Exclusions {
		coverage.ExclusionReasonCounts[item.Reason]++
		if catalogExclusionIsRegular(item) {
			regularExcluded++
		}
		// Collect reaches binary and size checks only after it has recognized a
		// supported language. Metadata exclusions occur before that decision.
		if item.Reason == inventory.ExcludedBinary || item.Reason == inventory.ExcludedOversized {
			supported++
		}
	}
	coverage.ExcludedPathCount = regularExcluded
	coverage.UnsupportedLanguageCount = coverage.ExclusionReasonCounts[inventory.ExcludedUnsupported]
	denominator := coverage.IndexedPathCount + regularExcluded
	if denominator > 0 && !catalog.Partial {
		coverage.PathCoverage = float64(coverage.IndexedPathCount) / float64(denominator)
	}
	languageDenominator := supported + coverage.UnsupportedLanguageCount
	if languageDenominator > 0 && !catalog.Partial {
		coverage.LanguageCoverage = float64(supported) / float64(languageDenominator)
	}
	return coverage
}

func catalogExclusionIsRegular(item model.SourceExclusion) bool {
	if item.Reason == inventory.ExcludedGit {
		return false
	}
	// Build records directory exclusions at the directory boundary. Generated
	// and vendored leaf files retain their extension, while these bare catalog
	// entries are directory metadata and do not contribute to path coverage.
	if (item.Reason == inventory.ExcludedGenerated || item.Reason == inventory.ExcludedVendored) && path.Ext(item.RelativePath) == "" {
		return false
	}
	return true
}

func languageForPath(relative string) string {
	extension := strings.ToLower(path.Ext(relative))
	for _, metadata := range inventory.ExtensionRegistry() {
		for _, candidate := range metadata.Extensions {
			if candidate == extension {
				return metadata.Language
			}
		}
	}
	return ""
}

type changeDocumentJSON struct {
	SchemaVersion                 string   `json:"schema_version"`
	PriorIndexIdentity            string   `json:"prior_index_identity"`
	BeforeRepositoryIdentity      string   `json:"before_repository_identity"`
	BeforeWorktreeIdentity        string   `json:"before_worktree_identity"`
	BeforeCommittedHead           string   `json:"before_committed_head"`
	BeforeDirtyOverlayFingerprint string   `json:"before_dirty_overlay_fingerprint"`
	AfterRepositoryIdentity       string   `json:"after_repository_identity"`
	AfterWorktreeIdentity         string   `json:"after_worktree_identity"`
	AfterCommittedHead            string   `json:"after_committed_head"`
	AfterDirtyOverlayFingerprint  string   `json:"after_dirty_overlay_fingerprint"`
	Level0ChangeManifestIdentity  string   `json:"level0_change_manifest_identity"`
	ChangedPaths                  []string `json:"changed_paths"`
}

var changeDocumentFields = []string{"schema_version", "prior_index_identity", "before_repository_identity", "before_worktree_identity", "before_committed_head", "before_dirty_overlay_fingerprint", "after_repository_identity", "after_worktree_identity", "after_committed_head", "after_dirty_overlay_fingerprint", "level0_change_manifest_identity", "changed_paths"}

var updateSHA = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
var updateObject = regexp.MustCompile(`^(?:[0-9a-f]{40}|[0-9a-f]{64})$`)

func decodeChangeDocument(raw []byte) (model.ChangeDocument, bool) {
	if len(raw) == 0 || !json.Valid(raw) || duplicateOrNonObject(raw) {
		return model.ChangeDocument{}, false
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || len(fields) != len(changeDocumentFields) {
		return model.ChangeDocument{}, false
	}
	for _, name := range changeDocumentFields {
		if _, present := fields[name]; !present {
			return model.ChangeDocument{}, false
		}
	}
	if bytes.Equal(bytes.TrimSpace(fields["changed_paths"]), []byte("null")) {
		return model.ChangeDocument{}, false
	}
	var decoded changeDocumentJSON
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&decoded); err != nil {
		return model.ChangeDocument{}, false
	}
	document := model.ChangeDocument{SchemaVersion: decoded.SchemaVersion, PriorIndexIdentity: decoded.PriorIndexIdentity, BeforeRepositoryIdentity: decoded.BeforeRepositoryIdentity, BeforeWorktreeIdentity: decoded.BeforeWorktreeIdentity, BeforeCommittedHead: decoded.BeforeCommittedHead, BeforeDirtyOverlayFingerprint: decoded.BeforeDirtyOverlayFingerprint, AfterRepositoryIdentity: decoded.AfterRepositoryIdentity, AfterWorktreeIdentity: decoded.AfterWorktreeIdentity, AfterCommittedHead: decoded.AfterCommittedHead, AfterDirtyOverlayFingerprint: decoded.AfterDirtyOverlayFingerprint, Level0ChangeManifestIdentity: decoded.Level0ChangeManifestIdentity, ChangedPaths: decoded.ChangedPaths}
	if !validChangeDocument(document) {
		return model.ChangeDocument{}, false
	}
	return document, true
}

func duplicateOrNonObject(raw []byte) bool {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return true
	}
	seen := map[string]struct{}{}
	for decoder.More() {
		name, err := decoder.Token()
		if err != nil {
			return true
		}
		key, ok := name.(string)
		if !ok {
			return true
		}
		if _, exists := seen[key]; exists {
			return true
		}
		seen[key] = struct{}{}
		var discard json.RawMessage
		if err := decoder.Decode(&discard); err != nil {
			return true
		}
	}
	_, err = decoder.Token()
	return err != nil || decoder.More()
}

func validChangeDocument(document model.ChangeDocument) bool {
	if document.SchemaVersion != "1" || !updateSHA.MatchString(document.PriorIndexIdentity) || !updateSHA.MatchString(document.BeforeRepositoryIdentity) || !updateSHA.MatchString(document.BeforeWorktreeIdentity) || !updateObject.MatchString(document.BeforeCommittedHead) || !updateSHA.MatchString(document.BeforeDirtyOverlayFingerprint) || !updateSHA.MatchString(document.AfterRepositoryIdentity) || !updateSHA.MatchString(document.AfterWorktreeIdentity) || !updateObject.MatchString(document.AfterCommittedHead) || !updateSHA.MatchString(document.AfterDirtyOverlayFingerprint) || !updateSHA.MatchString(document.Level0ChangeManifestIdentity) || len(document.ChangedPaths) > productionLimits().MaximumChangedPaths {
		return false
	}
	for index, changedPath := range document.ChangedPaths {
		if !safeUpdatePath(changedPath) || (index != 0 && document.ChangedPaths[index-1] >= changedPath) {
			return false
		}
	}
	return document.Level0ChangeManifestIdentity == changeManifestIdentity(document)
}

func changeManifestIdentity(document model.ChangeDocument) string {
	value := map[string]any{"schema_version": document.SchemaVersion, "prior_index_identity": document.PriorIndexIdentity, "before_repository_identity": document.BeforeRepositoryIdentity, "before_worktree_identity": document.BeforeWorktreeIdentity, "before_committed_head": document.BeforeCommittedHead, "before_dirty_overlay_fingerprint": document.BeforeDirtyOverlayFingerprint, "after_repository_identity": document.AfterRepositoryIdentity, "after_worktree_identity": document.AfterWorktreeIdentity, "after_committed_head": document.AfterCommittedHead, "after_dirty_overlay_fingerprint": document.AfterDirtyOverlayFingerprint, "changed_paths": document.ChangedPaths}
	encoded, _ := json.Marshal(value)
	digest := sha256.Sum256(append([]byte("taf-level0-change-manifest-v1\x00"), encoded...))
	return "sha256:" + hex.EncodeToString(digest[:])
}

func validUpdateBindings(document model.ChangeDocument, manifest model.Manifest, index string, request wire.Request) bool {
	before, after := manifest.Binding, request
	return document.PriorIndexIdentity == index && document.BeforeRepositoryIdentity == before.RepositoryIdentity && document.BeforeWorktreeIdentity == before.WorktreeIdentity && document.BeforeCommittedHead == before.CommittedHead && document.BeforeDirtyOverlayFingerprint == before.DirtyOverlayFingerprint && document.AfterRepositoryIdentity == after.RepositoryIdentity && document.AfterWorktreeIdentity == after.WorktreeIdentity && document.AfterCommittedHead == after.CommittedHead && document.AfterDirtyOverlayFingerprint == after.DirtyOverlayFingerprint
}
