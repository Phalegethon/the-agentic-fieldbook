package wire

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
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
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var envelope Envelope
	if err := decoder.Decode(&envelope); err != nil {
		return Envelope{}, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if err := requireEOF(decoder); err != nil {
		return Envelope{}, err
	}
	if err := ValidateRequest(envelope.Request); err != nil {
		return Envelope{}, err
	}
	return envelope, nil
}

func readFramed(reader io.Reader) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(reader, int64(policy.ProductionV1.MaximumWireBytes)+1))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if len(raw) == 0 || len(raw) > policy.ProductionV1.MaximumWireBytes || !utf8.Valid(raw) || raw[len(raw)-1] != '\n' || bytes.Count(raw, []byte{'\n'}) != 1 || bytes.Contains(raw, []byte{'\r'}) {
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
	if request.MaximumResults < 1 || request.MaximumResults > policy.ProductionV1.MaximumCollectionItems || !validBudget(request.MaximumModelOutputCharacters) {
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
	if result.SchemaVersion != "1" || !validID(result.RequestIdentity) || !validOperation(result.Operation) || !validStatus(result.Status) || result.ProviderIdentity != "taf.native.level1" || !validText(result.ProviderVersion, false) || !validSHA(result.RepositoryIdentity) || !validSHA(result.WorktreeIdentity) || !validObject(result.CommittedHead) || !validSHA(result.DirtyOverlayFingerprint) || !validFreshness(result.Freshness) || !validID(result.NextSafeAction) {
		return ErrInvalidWire
	}
	if result.IndexIdentity != nil && !validSHA(*result.IndexIdentity) {
		return ErrInvalidWire
	}
	if result.IndexIdentity == nil && !(result.Operation == Estimate || (result.Operation == Build && result.Status != Ready)) {
		return ErrInvalidWire
	}
	if len(result.ParserVersions) > policy.ProductionV1.MaximumCollectionItems || len(result.Findings) > policy.ProductionV1.MaximumCollectionItems || len(result.Warnings) > policy.ProductionV1.MaximumCollectionItems || result.ReturnedCount != len(result.Findings) || result.OmittedCount < 0 || result.Truncated != (result.OmittedCount > 0) {
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
	for key, value := range result.Coverage.ExclusionReasonCounts {
		if !validID(key) || value < 0 {
			return ErrInvalidWire
		}
	}
	for index, finding := range result.Findings {
		if err := validateFinding(finding, index+1, result.Freshness); err != nil {
			return err
		}
	}
	if (result.Status == Stale || result.Status == Unsupported || result.Status == Error) && len(result.Findings) != 0 {
		return ErrInvalidWire
	}
	return nil
}

func validateFinding(finding Finding, rank int, freshness string) error {
	if finding.Rank != rank || !validSHA(finding.ResultIdentity) || !validPath(finding.Path) || finding.StartLine < 1 || finding.EndLine < finding.StartLine || !validText(finding.Language, false) || !oneOf(finding.RecordKind, "module", "definition", "import", "entry-point", "configuration", "heading", "document-chunk") || !oneOf(finding.SourceType, "source", "document", "configuration") || !validText(finding.QualifiedName, true) || !validText(finding.ExtractionMethod, false) || !validText(finding.Preview, true) || !oneOf(finding.EvidenceClass, "verified", "inferred", "uncertain") {
		return ErrInvalidWire
	}
	if freshness != "exact" && finding.EvidenceClass == "verified" {
		return ErrInvalidWire
	}
	return nil
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
	for _, item := range AllOperations {
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
	if len(values) > policy.ProductionV1.MaximumCollectionItems {
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
