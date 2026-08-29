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
	if err := requireKeys(request, required, map[string]bool{"index_identity": true, "query": true}); err != nil {
		return err
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
	if envelope.Phase != "query" || !validText(envelope.RepositoryRoot, false) || !filepath.IsAbs(envelope.RepositoryRoot) || !validText(envelope.StateRoot, false) || !filepath.IsAbs(envelope.StateRoot) {
		return ErrInvalidWire
	}
	if envelope.ChangedPathsDocument != nil && !validText(*envelope.ChangedPathsDocument, false) {
		return ErrInvalidWire
	}
	return nil
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
	if request.SchemaVersion != "1" || !validID(request.RequestIdentity) || !validID(request.ConsumerIdentity) || !validOperation(request.Operation) || !validSHA(request.RepositoryIdentity) || !validSHA(request.WorktreeIdentity) || !validObject(request.CommittedHead) || !validSHA(request.DirtyOverlayFingerprint) || request.ProviderIdentity != "taf.native.level1" || !validID(request.RequiredCapability) || !validFreshness(request.MinimumFreshness) {
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
	if request.Operation == SourceSnippets {
		if request.Query != nil || len(request.ResultIdentities) == 0 {
			return ErrInvalidWire
		}
	} else if len(request.ResultIdentities) != 0 {
		return ErrInvalidWire
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
	return nil
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
	if result.SchemaVersion != "1" || !validID(result.RequestIdentity) || !validOperation(result.Operation) || !validStatus(result.Status) || result.ProviderIdentity != "taf.native.level1" || !validText(result.ProviderVersion, false) || !validSHA(result.RepositoryIdentity) || !validSHA(result.WorktreeIdentity) || !validObject(result.CommittedHead) || !validSHA(result.DirtyOverlayFingerprint) || !validFreshness(result.Freshness) || !validID(result.NextSafeAction) {
		return ErrInvalidWire
	}
	if result.IndexIdentity != nil && !validSHA(*result.IndexIdentity) {
		return ErrInvalidWire
	}
	if result.IndexIdentity == nil && !(result.Operation == Estimate || (result.Operation == Build && result.Status != Ready)) {
		return ErrInvalidWire
	}
	if result.ParserVersions == nil || result.Coverage.ExclusionReasonCounts == nil || result.Findings == nil || result.Warnings == nil || len(result.ParserVersions) > policy.ProductionLimits().MaximumCollectionItems || len(result.Findings) > policy.ProductionLimits().MaximumCollectionItems || len(result.Warnings) > policy.ProductionLimits().MaximumCollectionItems || result.ReturnedCount != len(result.Findings) || !validCounter(result.ReturnedCount) || !validCounter(result.OmittedCount) || result.Truncated != (result.OmittedCount > 0) {
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
		if err := validateFinding(finding, index+1, result.Freshness); err != nil {
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
		fmt.Fprintf(&text, "FINDING %s %s %s:%d-%d %s %s method=%s\n", finding.EvidenceClass, finding.RecordKind, finding.Path, finding.StartLine, finding.EndLine, finding.Language, finding.QualifiedName, finding.ExtractionMethod)
		if finding.Preview != "" || result.Operation == SourceSnippets {
			for _, line := range strings.Split(finding.Preview, "\n") {
				fmt.Fprintf(&text, "PREVIEW %s\n", line)
			}
		}
	}
	fmt.Fprintf(&text, "NEXT %s\n", result.NextSafeAction)
	return utf8.RuneCountInString(text.String())
}

func validateFinding(finding Finding, rank int, freshness string) error {
	if finding.Rank != rank || !validSHA(finding.ResultIdentity) || !validPath(finding.Path) || finding.StartLine < 1 || finding.EndLine < finding.StartLine || !validCounter(finding.StartLine) || !validCounter(finding.EndLine) || !validText(finding.Language, false) || !oneOf(finding.RecordKind, "module", "definition", "import", "entry-point", "configuration", "heading", "document-chunk") || !oneOf(finding.SourceType, "source", "document", "configuration") || !validText(finding.QualifiedName, true) || !validText(finding.ExtractionMethod, false) || !validPreview(finding.Preview) || !oneOf(finding.EvidenceClass, "verified", "inferred", "uncertain") {
		return ErrInvalidWire
	}
	if freshness != "exact" && finding.EvidenceClass == "verified" {
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
