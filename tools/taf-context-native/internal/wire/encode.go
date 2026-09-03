package wire

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

func EncodeResult(writer io.Writer, result Result) error {
	if err := validateResult(result); err != nil {
		return err
	}
	encoded, err := marshalCanonical(marshalableResult(result))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if len(encoded)+1 > policy.ProductionLimits().MaximumStdoutBytes {
		return ErrInvalidWire
	}
	_, err = writer.Write(append(encoded, '\n'))
	return err
}

// MeasureResult validates all non-budget invariants and returns only the two
// measurements a bounded renderer needs; it never exposes transport bytes.
func MeasureResult(result Result) (int, int, error) {
	if err := validateResultWithoutBudgets(result); err != nil {
		return 0, 0, err
	}
	encoded, err := marshalCanonical(marshalableResult(result))
	if err != nil {
		return 0, 0, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	return len(encoded) + 1, renderedOutputCharacters(result), nil
}

// OutputCharacters returns the frozen model-visible character calculation.
// It deliberately shares validation's calculation rather than duplicating it
// in callers that need to fit a result before encoding it.
func OutputCharacters(result Result) int { return renderedOutputCharacters(result) }

// findingV1 is the frozen schema-1 finding shape: the schema-2 edge fields are
// absent rather than empty, so schema-1 frames stay byte-identical.
type findingV1 struct {
	Rank             int    `json:"rank"`
	ResultIdentity   string `json:"result_identity"`
	Path             string `json:"path"`
	StartLine        int    `json:"start_line"`
	EndLine          int    `json:"end_line"`
	Language         string `json:"language"`
	RecordKind       string `json:"record_kind"`
	SourceType       string `json:"source_type"`
	QualifiedName    string `json:"qualified_name"`
	ExtractionMethod string `json:"extraction_method"`
	EvidenceClass    string `json:"evidence_class"`
	Preview          string `json:"preview"`
}

// resultV1 shadows Result.Findings with the schema-1 finding shape; every other
// field is inherited, so a new result field cannot silently vanish from schema 1.
type resultV1 struct {
	Result
	Findings []findingV1 `json:"findings"`
}

// marshalableResult picks the marshaling shape for the result's schema version.
func marshalableResult(result Result) any {
	if result.SchemaVersion != "1" {
		return result
	}
	findings := make([]findingV1, 0, len(result.Findings))
	for _, finding := range result.Findings {
		findings = append(findings, findingV1{
			Rank: finding.Rank, ResultIdentity: finding.ResultIdentity, Path: finding.Path,
			StartLine: finding.StartLine, EndLine: finding.EndLine, Language: finding.Language,
			RecordKind: finding.RecordKind, SourceType: finding.SourceType, QualifiedName: finding.QualifiedName,
			ExtractionMethod: finding.ExtractionMethod, EvidenceClass: finding.EvidenceClass, Preview: finding.Preview,
		})
	}
	return resultV1{Result: result, Findings: findings}
}

func marshalCanonical(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var decoded any
	if err := decoder.Decode(&decoded); err != nil {
		return nil, err
	}
	return json.Marshal(decoded)
}
