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
	encoded, err := marshalCanonical(result)
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
	encoded, err := marshalCanonical(result)
	if err != nil {
		return 0, 0, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	return len(encoded) + 1, renderedOutputCharacters(result), nil
}

// OutputCharacters returns the frozen model-visible character calculation.
// It deliberately shares validation's calculation rather than duplicating it
// in callers that need to fit a result before encoding it.
func OutputCharacters(result Result) int { return renderedOutputCharacters(result) }

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
