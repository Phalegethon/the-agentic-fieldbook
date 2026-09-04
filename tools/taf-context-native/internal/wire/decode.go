package wire

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

var (
	canonicalID = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
	sha256ID    = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	objectID    = regexp.MustCompile(`^(?:[0-9a-f]{40}|[0-9a-f]{64})$`)
)

func DecodeEnvelope(reader io.Reader) (Envelope, error) {
	raw, err := readFramed(reader)
	if err != nil {
		return Envelope{}, err
	}
	if err := rejectDuplicateKeys(raw); err != nil {
		return Envelope{}, err
	}
	if err := validateEnvelopeShape(raw); err != nil {
		return Envelope{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var envelope Envelope
	if err := decoder.Decode(&envelope); err != nil {
		return Envelope{}, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if err := requireEOF(decoder); err != nil {
		return Envelope{}, err
	}
	if err := validateEnvelope(envelope); err != nil {
		return Envelope{}, err
	}
	if err := ValidateRequest(envelope.Request); err != nil {
		return Envelope{}, err
	}
	return envelope, nil
}

func validateEnvelopeShape(raw []byte) error {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if err := requireKeys(envelope, []string{"phase", "repository_root", "state_root", "changed_paths_document", "request"}, map[string]bool{"changed_paths_document": true}); err != nil {
		return err
	}
	for _, field := range []string{"phase", "repository_root", "state_root", "request"} {
		if isNull(envelope[field]) {
			return ErrInvalidWire
		}
	}
	var request map[string]json.RawMessage
	if err := json.Unmarshal(envelope["request"], &request); err != nil {
		return ErrInvalidWire
	}
	required := []string{"schema_version", "request_identity", "consumer_identity", "operation", "repository_identity", "worktree_identity", "committed_head", "dirty_overlay_fingerprint", "provider_identity", "index_identity", "required_capability", "minimum_freshness", "query", "result_identities", "filters", "maximum_results", "maximum_model_output_characters", "allow_inferred"}
	nullable := map[string]bool{"index_identity": true, "query": true}
	// The key set is schema-dependent: schema 2 requires direction (possibly
	// null), schemas 3 and 4 require direction and changed_ranges (both
	// possibly null, and schema 4 always null), and schema 1 must not carry
	// either. A malformed schema_version falls through to the schema-1 key set
	// and the typed validator rejects it.
	var schemaVersion string
	if err := json.Unmarshal(request["schema_version"], &schemaVersion); err != nil {
		schemaVersion = ""
	}
	switch schemaVersion {
	case "2":
		required = append(required, "direction")
		nullable["direction"] = true
	case "3", "4":
		required = append(required, "direction", "changed_ranges")
		nullable["direction"], nullable["changed_ranges"] = true, true
	}
	if err := requireKeys(request, required, nullable); err != nil {
		return err
	}
	if raw, ok := request["changed_ranges"]; ok && !isNull(raw) {
		if err := validateChangedRangesShape(raw); err != nil {
			return err
		}
	}
	var filters map[string]json.RawMessage
	if err := json.Unmarshal(request["filters"], &filters); err != nil {
		return ErrInvalidWire
	}
	return requireKeys(filters, []string{"path_prefixes", "languages", "symbol_kinds", "source_types"}, nil)
}

func requireKeys(value map[string]json.RawMessage, required []string, nullable map[string]bool) error {
	if len(value) != len(required) {
		return ErrInvalidWire
	}
	for _, field := range required {
		raw, ok := value[field]
		if !ok || (!nullable[field] && isNull(raw)) {
			return ErrInvalidWire
		}
	}
	return nil
}

func isNull(raw json.RawMessage) bool { return len(raw) == 0 || bytes.Equal(raw, []byte("null")) }

func validateEnvelope(envelope Envelope) error {
	if !validPhaseOperation(envelope.Phase, envelope.Request.Operation) || !validText(envelope.RepositoryRoot, false) || !filepath.IsAbs(envelope.RepositoryRoot) || !validText(envelope.StateRoot, false) || !filepath.IsAbs(envelope.StateRoot) {
		return ErrInvalidWire
	}
	if envelope.ChangedPathsDocument != nil && !validText(*envelope.ChangedPathsDocument, false) {
		return ErrInvalidWire
	}
	return nil
}

func validPhaseOperation(phase string, operation Operation) bool {
	switch phase {
	case "build":
		return operation == Build
	case "estimate":
		return operation == Estimate
	case "inspect":
		return operation == StatusOperation
	case "metrics":
		return operation == Metrics
	case "update":
		return operation == Update
	case "query":
		return operation == RepositoryMap || operation == SearchDocs || operation == SearchSymbols || operation == SourceSnippets || operation == RelatedSymbols || operation == ChangedSymbols || operation == RepositoryOverview
	default:
		return false
	}
}

func readFramed(reader io.Reader) ([]byte, error) {
	limits := policy.ProductionLimits()
	raw, err := io.ReadAll(io.LimitReader(reader, int64(limits.MaximumWireBytes)+1))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if len(raw) == 0 || len(raw) > limits.MaximumWireBytes || !utf8.Valid(raw) || raw[len(raw)-1] != '\n' || bytes.Count(raw, []byte{'\n'}) != 1 || bytes.Contains(raw, []byte{'\r'}) {
		return nil, ErrInvalidWire
	}
	return raw[:len(raw)-1], nil
}

func rejectDuplicateKeys(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := walkJSON(decoder); err != nil {
		return err
	}
	return requireEOF(decoder)
}

func walkJSON(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	switch value := token.(type) {
	case json.Delim:
		switch value {
		case '{':
			seen := map[string]struct{}{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return fmt.Errorf("%w: %v", ErrInvalidWire, err)
				}
				key, ok := keyToken.(string)
				if !ok {
					return ErrInvalidWire
				}
				if _, exists := seen[key]; exists {
					return ErrDuplicateKey
				}
				seen[key] = struct{}{}
				if err := walkJSON(decoder); err != nil {
					return err
				}
			}
			if _, err := decoder.Token(); err != nil {
				return fmt.Errorf("%w: %v", ErrInvalidWire, err)
			}
		case '[':
			for decoder.More() {
				if err := walkJSON(decoder); err != nil {
					return err
				}
			}
			if _, err := decoder.Token(); err != nil {
				return fmt.Errorf("%w: %v", ErrInvalidWire, err)
			}
		}
	}
	return nil
}

func requireEOF(decoder *json.Decoder) error {
	if token, err := decoder.Token(); err != io.EOF {
		if err != nil {
			return fmt.Errorf("%w: %v", ErrInvalidWire, err)
		}
		return fmt.Errorf("%w: unexpected token %v", ErrInvalidWire, token)
	}
	return nil
}

func ValidateRequest(request Request) error {
	if !validSchemaVersion(request.SchemaVersion) || !validID(request.RequestIdentity) || !validID(request.ConsumerIdentity) || !validOperation(request.Operation) || !validSHA(request.RepositoryIdentity) || !validSHA(request.WorktreeIdentity) || !validObject(request.CommittedHead) || !validSHA(request.DirtyOverlayFingerprint) || request.ProviderIdentity != "taf-context" || !validID(request.RequiredCapability) || !validFreshness(request.MinimumFreshness) {
		return ErrInvalidWire
	}
	if request.RequiredCapability != string(request.Operation) {
		return ErrRequiredCapability
	}
	if (request.IndexIdentity != nil && !validSHA(*request.IndexIdentity)) || (request.Query != nil && !validText(*request.Query, false)) {
		return ErrInvalidWire
	}
	if request.MaximumResults < 1 || request.MaximumResults > policy.ProductionLimits().MaximumCollectionItems || !validBudget(request.MaximumModelOutputCharacters) {
		return ErrInvalidWire
	}
	if err := validateFilters(request.Filters); err != nil {
		return err
	}
	if !sortedSHA(request.ResultIdentities) {
		return ErrInvalidWire
	}

	queryOperation := request.Operation == SearchSymbols || request.Operation == SearchDocs
	if queryOperation != (request.Query != nil) {
		return ErrInvalidWire
	}
	if !validSchemaOperation(request.SchemaVersion, request.Operation) {
		return ErrInvalidWire
	}
	if err := validateDirection(request); err != nil {
		return err
	}
	if err := validateChangedRanges(request); err != nil {
		return err
	}
	switch request.Operation {
	case SourceSnippets:
		if request.Query != nil || len(request.ResultIdentities) == 0 {
			return ErrInvalidWire
		}
	case RelatedSymbols:
		if request.Query != nil || len(request.ResultIdentities) == 0 || len(request.ResultIdentities) > maximumRelatedAnchors {
			return ErrInvalidWire
		}
	default:
		if len(request.ResultIdentities) != 0 {
			return ErrInvalidWire
		}
	}
	if request.Operation == Estimate || request.Operation == Build {
		if request.IndexIdentity != nil {
			return ErrInvalidWire
		}
	} else if request.IndexIdentity == nil {
		return ErrInvalidWire
	}
	if isControlOperation(request.Operation) && !filtersEmpty(request.Filters) {
		return ErrInvalidWire
	}
	// The overview counts files, not symbols, so the two symbol-shaped filters
	// would promise a narrowing it cannot perform.
	if request.Operation == RepositoryOverview && (len(request.Filters.SymbolKinds) != 0 || len(request.Filters.SourceTypes) != 0) {
		return ErrInvalidWire
	}
	return nil
}

// maximumRelatedAnchors bounds the anchors a single relationship request may
// carry, keeping the query-time edge resolution work predictable.
const maximumRelatedAnchors = 16

// The overview returns one row per directory group it found and folds nothing
// away, so the repository's own shape is what makes the table wide: a group
// holds at least one indexed path, which makes one row per indexed path the
// theoretical maximum. 4096 is well above any directory table a repository
// produces and well below policy.MaximumEligiblePaths, and the transport byte
// cap ends an honest wide table long before this bound does — so this one is
// here to reject a payload that is no longer a directory table at all.
const MaximumOverviewGroups = 4096

// The changed selector is bounded independently of the collection limit: a
// change set names many more paths than a result may return, and the two
// ceilings below keep one request's intersection work predictable.
const (
	maximumChangedPaths         = 200
	maximumChangedRangesPerPath = 64
)

// validSchemaOperation binds the three schema-gated operations to the schema
// that introduced them: related-symbols exists only in schema 2, changed-symbols
// only in schema 3, and repository-overview only in schema 4, so none of them
// can appear under the frozen schema 1 nor leak into another's schema. Every
// other operation is schema-agnostic and may travel under any known schema.
func validSchemaOperation(schemaVersion string, operation Operation) bool {
	switch operation {
	case RelatedSymbols:
		return schemaVersion == "2"
	case ChangedSymbols:
		return schemaVersion == "3"
	case RepositoryOverview:
		return schemaVersion == "4"
	default:
		return true
	}
}

// validateChangedRanges enforces the schema-3 change selector: only schema 3
// carries changed ranges, and they are present exactly for changed-symbols.
// Paths are sorted and unique, and every path's spans are bounded, ascending,
// and non-overlapping, so the engine can intersect them by a single scan.
func validateChangedRanges(request Request) error {
	if request.SchemaVersion != "3" {
		if request.ChangedRanges != nil {
			return ErrInvalidWire
		}
		return nil
	}
	if (request.ChangedRanges != nil) != (request.Operation == ChangedSymbols) {
		return ErrInvalidWire
	}
	if request.ChangedRanges == nil {
		return nil
	}
	entries := *request.ChangedRanges
	if len(entries) > maximumChangedPaths {
		return ErrInvalidWire
	}
	for index, entry := range entries {
		if !validPath(entry.Path) || (index > 0 && entries[index-1].Path >= entry.Path) {
			return ErrInvalidWire
		}
		if len(entry.Ranges) > maximumChangedRangesPerPath {
			return ErrInvalidWire
		}
		for position, span := range entry.Ranges {
			if span[0] < 1 || span[1] < span[0] || !validCounter(span[1]) {
				return ErrInvalidWire
			}
			if position > 0 && entry.Ranges[position-1][1] >= span[0] {
				return ErrInvalidWire
			}
		}
	}
	return nil
}

// validateChangedRangesShape keeps the selector strict on the wire: every entry
// spells both keys out and every span is a two-element array, so a malformed
// span cannot be silently truncated or zero-filled by typed decoding.
func validateChangedRangesShape(raw json.RawMessage) error {
	var entries []map[string]json.RawMessage
	if err := json.Unmarshal(raw, &entries); err != nil {
		return ErrInvalidWire
	}
	for _, entry := range entries {
		if err := requireKeys(entry, []string{"path", "ranges"}, nil); err != nil {
			return err
		}
		var spans []json.RawMessage
		if err := json.Unmarshal(entry["ranges"], &spans); err != nil {
			return ErrInvalidWire
		}
		for _, span := range spans {
			var bounds []json.Number
			if err := json.Unmarshal(span, &bounds); err != nil || len(bounds) != 2 {
				return ErrInvalidWire
			}
		}
	}
	return nil
}

// validateDirection enforces the schema-2 relationship selector: only schema 2
// carries a direction, and there it is present exactly for related-symbols.
// The operation-to-schema binding itself lives in validSchemaOperation.
func validateDirection(request Request) error {
	if request.SchemaVersion != "2" {
		if request.Direction != nil {
			return ErrInvalidWire
		}
		return nil
	}
	if (request.Direction != nil) != (request.Operation == RelatedSymbols) {
		return ErrInvalidWire
	}
	if request.Direction != nil && !oneOf(*request.Direction, "callers", "callees", "importers", "imports") {
		return ErrInvalidWire
	}
	return nil
}

// validateOverview enforces the schema-4 overview payload. Only schema 4
// carries the two result keys, and there both are required for every operation:
// they belong to the schema, not to the operation that introduced them. Struct
// marshaling omits a nil group list and a nil summary, so a schema-4 result
// that left either unset would travel without a key the schema promises.
func validateOverview(result Result) error {
	if result.SchemaVersion != "4" {
		if result.Groups != nil || result.Overview != nil {
			return ErrInvalidWire
		}
		return nil
	}
	if result.Groups == nil || result.Overview == nil {
		return ErrInvalidWire
	}
	groups := *result.Groups
	if len(groups) > MaximumOverviewGroups {
		return ErrInvalidWire
	}
	for _, group := range groups {
		if err := validateOverviewGroup(group); err != nil {
			return err
		}
	}
	summary := *result.Overview
	if !validOverviewRoot(summary.Root) || !validCounter(summary.CountedFileCount) || !validCounter(summary.OtherGroupCount) {
		return ErrInvalidWire
	}
	return nil
}

// validateOverviewGroup keeps one group row honest: a prefix of one of the four
// admitted shapes, counters that cannot be negative, a language list ordered by
// count descending then name ascending (which also rejects a repeated name),
// and a representative the caller can cite back — absent exactly for the "*"
// row, which sums several directories and represents none of them. The engine
// emits no such row; a consumer folding the table to an output budget does.
func validateOverviewGroup(group OverviewGroup) error {
	if !validOverviewPrefix(group.PathPrefix) || !validCounter(group.Depth) || !validCounter(group.FileCount) || !validCounter(group.DefinitionCount) || !validCounter(group.EntryPointCount) || !validCounter(group.DocumentCount) || !validCounter(group.ConfigurationCount) {
		return ErrInvalidWire
	}
	if group.Languages == nil || len(group.Languages) > policy.ProductionLimits().MaximumCollectionItems {
		return ErrInvalidWire
	}
	for index, language := range group.Languages {
		if !validText(language.Language, false) || !validCounter(language.FileCount) {
			return ErrInvalidWire
		}
		if index == 0 {
			continue
		}
		previous := group.Languages[index-1]
		if previous.FileCount < language.FileCount || (previous.FileCount == language.FileCount && previous.Language >= language.Language) {
			return ErrInvalidWire
		}
	}
	if group.PathPrefix == "*" {
		if group.RepresentativeIdentity != nil {
			return ErrInvalidWire
		}
		return nil
	}
	if group.RepresentativeIdentity != nil && !validSHA(*group.RepresentativeIdentity) {
		return ErrInvalidWire
	}
	return nil
}

// validOverviewPrefix admits the four group prefix shapes: "." for the files at
// the repository root, "*" for a consumer's folded tail, "<directory>/" for a
// directory subtree, and "<directory>/." for the files directly inside a
// directory a split replaced by its children.
func validOverviewPrefix(value string) bool {
	if value == "." || value == "*" {
		return true
	}
	if !validPath(value) || (!strings.HasSuffix(value, "/") && !strings.HasSuffix(value, "/.")) {
		return false
	}
	directory := strings.TrimSuffix(strings.TrimSuffix(value, "."), "/")
	if directory == "" {
		return false
	}
	for _, segment := range strings.Split(directory, "/") {
		if segment == "" || segment == "." {
			return false
		}
	}
	return true
}

// validOverviewRoot admits the repository root as "" and every other overview
// root as a normalized directory prefix, so a consumer can join it to a group
// prefix without guessing where a separator is missing.
func validOverviewRoot(value string) bool {
	return value == "" || (strings.HasSuffix(value, "/") && validOverviewPrefix(value))
}

func validateResult(result Result) error {
	if err := validateResultWithoutBudgets(result); err != nil {
		return err
	}
	if result.OutputCharacters > 12000 {
		return ErrInvalidWire
	}
	return nil
}

func validateResultWithoutBudgets(result Result) error {
	if !validSchemaVersion(result.SchemaVersion) || !validID(result.RequestIdentity) || !validOperation(result.Operation) || !validSchemaOperation(result.SchemaVersion, result.Operation) || !validStatus(result.Status) || result.ProviderIdentity != "taf-context" || !validText(result.ProviderVersion, false) || !validSHA(result.RepositoryIdentity) || !validSHA(result.WorktreeIdentity) || !validObject(result.CommittedHead) || !validSHA(result.DirtyOverlayFingerprint) || !validFreshness(result.Freshness) || !validID(result.NextSafeAction) {
		return ErrInvalidWire
	}
	if result.IndexIdentity != nil && !validSHA(*result.IndexIdentity) {
		return ErrInvalidWire
	}
	if result.IndexIdentity == nil && !(result.Operation == Estimate || (result.Operation == Build && result.Status != Ready)) {
		return ErrInvalidWire
	}
	// Truncated may be true with OmittedCount == 0 (an exhausted search whose
	// omissions could not be counted); it must never be false with
	// OmittedCount > 0 (a counted omission the wire failed to disclose).
	if result.ParserVersions == nil || result.Coverage.ExclusionReasonCounts == nil || result.Findings == nil || result.Warnings == nil || len(result.ParserVersions) > policy.ProductionLimits().MaximumCollectionItems || len(result.Findings) > policy.ProductionLimits().MaximumCollectionItems || len(result.Warnings) > policy.ProductionLimits().MaximumCollectionItems || result.ReturnedCount != len(result.Findings) || !validCounter(result.ReturnedCount) || !validCounter(result.OmittedCount) || (result.OmittedCount > 0 && !result.Truncated) {
		return ErrInvalidWire
	}
	if !validCounter(result.Coverage.IndexedPathCount) || !validCounter(result.Coverage.ExcludedPathCount) || !validCounter(result.Coverage.UnsupportedLanguageCount) || !validCounter(result.Coverage.ParseFailureCount) || len(result.Coverage.ExclusionReasonCounts) > policy.ProductionLimits().MaximumCollectionItems || !validCounter(result.OutputCharacters) || result.OutputCharacters != renderedOutputCharacters(result) {
		return ErrInvalidWire
	}
	if result.Coverage.PathCoverage < 0 || result.Coverage.PathCoverage > 1 || result.Coverage.LanguageCoverage < 0 || result.Coverage.LanguageCoverage > 1 || math.IsNaN(result.Coverage.PathCoverage) || math.IsInf(result.Coverage.PathCoverage, 0) || math.IsNaN(result.Coverage.LanguageCoverage) || math.IsInf(result.Coverage.LanguageCoverage, 0) {
		return ErrInvalidWire
	}
	if !sortedText(result.Warnings) {
		return ErrInvalidWire
	}
	if err := validateOverview(result); err != nil {
		return err
	}
	for key, value := range result.ParserVersions {
		if !validID(key) || !validText(value, false) {
			return ErrInvalidWire
		}
	}
	identities := make(map[string]struct{}, len(result.Findings))
	for key, value := range result.Coverage.ExclusionReasonCounts {
		if !validID(key) || !validCounter(value) {
			return ErrInvalidWire
		}
	}
	for index, finding := range result.Findings {
		if err := validateFinding(finding, index+1, result.Freshness, result.SchemaVersion, result.Operation); err != nil {
			return err
		}
		if _, exists := identities[finding.ResultIdentity]; exists {
			return ErrInvalidWire
		}
		identities[finding.ResultIdentity] = struct{}{}
	}
	if (result.Status == Stale || result.Status == Unsupported || result.Status == Error) && len(result.Findings) != 0 {
		return ErrInvalidWire
	}
	return nil
}

func validCounter(value int) bool { return value >= 0 && value <= 1<<31-1 }

func renderedOutputCharacters(result Result) int {
	var text strings.Builder
	fmt.Fprintf(&text, "LEVEL1 status=%s operation=%s freshness=%s returned=%d omitted=%d warnings=%d\n", result.Status, result.Operation, result.Freshness, len(result.Findings), result.OmittedCount, len(result.Warnings))
	fmt.Fprintf(&text, "COVERAGE paths=%.3f languages=%.3f unsupported=%d parse_failures=%d\n", result.Coverage.PathCoverage, result.Coverage.LanguageCoverage, result.Coverage.UnsupportedLanguageCount, result.Coverage.ParseFailureCount)
	for _, finding := range result.Findings {
		fmt.Fprintf(&text, "FINDING %s %s %s:%d-%d %s %s method=%s", finding.EvidenceClass, finding.RecordKind, finding.Path, finding.StartLine, finding.EndLine, finding.Language, finding.QualifiedName, finding.ExtractionMethod)
		if result.SchemaVersion == "2" && finding.Relation != "" {
			fmt.Fprintf(&text, " relation=%s edge=%s ref=%dx%d", finding.Relation, finding.EdgeEvidence, finding.ReferenceLine, finding.ReferenceCount)
		}
		text.WriteString("\n")
		if finding.Preview != "" || result.Operation == SourceSnippets {
			for _, line := range strings.Split(finding.Preview, "\n") {
				fmt.Fprintf(&text, "PREVIEW %s\n", line)
			}
		}
	}
	fmt.Fprintf(&text, "NEXT %s\n", result.NextSafeAction)
	return utf8.RuneCountInString(text.String())
}

func validateFinding(finding Finding, rank int, freshness string, schemaVersion string, operation Operation) error {
	if finding.Rank != rank || !validSHA(finding.ResultIdentity) || !validPath(finding.Path) || finding.StartLine < 1 || finding.EndLine < finding.StartLine || !validCounter(finding.StartLine) || !validCounter(finding.EndLine) || !validText(finding.Language, false) || !oneOf(finding.RecordKind, "module", "definition", "import", "entry-point", "configuration", "heading", "document-chunk") || !oneOf(finding.SourceType, "source", "document", "configuration") || !validText(finding.QualifiedName, true) || !validText(finding.ExtractionMethod, false) || !validPreview(finding.Preview) || !oneOf(finding.EvidenceClass, "verified", "inferred", "uncertain") {
		return ErrInvalidWire
	}
	if freshness != "exact" && finding.EvidenceClass == "verified" {
		return ErrInvalidWire
	}
	return validateEdgeFields(finding, schemaVersion, operation)
}

// validateEdgeFields keeps the schemas honest in both directions: schema 1
// carries no edge data at all (it would be silently dropped by the encoder),
// schema 3 resolves no edges, and schema 2 admits only the frozen relation and
// evidence vocabularies, and only on the one operation that resolves edges.
// Every other result leaves the four fields empty.
func validateEdgeFields(finding Finding, schemaVersion string, operation Operation) error {
	if schemaVersion != "2" || operation != RelatedSymbols {
		if finding.Relation != "" || finding.EdgeEvidence != "" || finding.ReferenceLine != 0 || finding.ReferenceCount != 0 {
			return ErrInvalidWire
		}
		return nil
	}
	if !oneOf(finding.Relation, "", "call", "import") || !oneOf(finding.EdgeEvidence, "", "verified", "inferred") || !validCounter(finding.ReferenceLine) || !validCounter(finding.ReferenceCount) {
		return ErrInvalidWire
	}
	return nil
}

// Previews are bounded evidence fields, unlike metadata text: source snippets
// may span several lines and need the full 2k–12k model-output range. The
// result/output validators still enforce the global rendered character and
// serialized-byte ceilings before a frame is emitted.
func validPreview(value string) bool {
	return utf8.ValidString(value) && utf8.RuneCountInString(value) <= 12000 && !strings.ContainsAny(value, "\x00\r")
}

func validID(value string) bool     { return canonicalID.MatchString(value) }
func validSHA(value string) bool    { return sha256ID.MatchString(value) }
func validObject(value string) bool { return objectID.MatchString(value) }
func validText(value string, empty bool) bool {
	return utf8.ValidString(value) && len(value) <= 512 && (empty || value != "") && !strings.ContainsAny(value, "\x00\n\r")
}
func validPath(value string) bool {
	return validText(value, false) && !strings.HasPrefix(value, "/") && !regexp.MustCompile(`^[A-Za-z]:`).MatchString(value) && !strings.Contains(value, `\\`) && !strings.Contains("/"+value+"/", "/../") && value == strings.TrimPrefix(value, "./")
}
func validOperation(value Operation) bool {
	for _, item := range operations {
		if value == item {
			return true
		}
	}
	return false
}
func validStatus(value Status) bool {
	return value == Ready || value == Partial || value == Stale || value == Unsupported || value == Error
}
func validFreshness(value string) bool {
	return oneOf(value, "exact", "commit-fresh-worktree-stale", "incrementally-stale", "structurally-stale", "partial", "unknown", "unusable")
}
func validSchemaVersion(value string) bool { return oneOf(value, "1", "2", "3", "4") }
func validBudget(value int) bool {
	return value == 2000 || value == 4000 || value == 8000 || value == 12000
}
func oneOf(value string, allowed ...string) bool {
	for _, item := range allowed {
		if value == item {
			return true
		}
	}
	return false
}
func isControlOperation(value Operation) bool {
	return value == Estimate || value == Build || value == Update || value == StatusOperation || value == Metrics
}
func filtersEmpty(filters Filters) bool {
	return len(filters.PathPrefixes) == 0 && len(filters.Languages) == 0 && len(filters.SymbolKinds) == 0 && len(filters.SourceTypes) == 0
}
func sortedText(values []string) bool {
	return sortedUnique(values, func(value string) bool { return validText(value, false) })
}
func sortedSHA(values []string) bool { return sortedUnique(values, validSHA) }
func sortedUnique(values []string, valid func(string) bool) bool {
	if len(values) > policy.ProductionLimits().MaximumCollectionItems {
		return false
	}
	for index, value := range values {
		if !valid(value) || (index > 0 && values[index-1] >= value) {
			return false
		}
	}
	return true
}
func validateFilters(filters Filters) error {
	if !sortedUnique(filters.PathPrefixes, validPath) || !sortedText(filters.Languages) || !sortedUnique(filters.SymbolKinds, validID) || !sortedUnique(filters.SourceTypes, func(value string) bool { return oneOf(value, "source", "document", "configuration") }) {
		return ErrInvalidWire
	}
	return nil
}
