package wire

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

func EncodeResult(writer io.Writer, result Result) error {
	encoded, err := MarshalResult(result)
	if err != nil {
		return err
	}
	if len(encoded)+1 > policy.ProductionLimits().MaximumStdoutBytes {
		return ErrInvalidWire
	}
	_, err = writer.Write(append(encoded, '\n'))
	return err
}

// MarshalResult validates every structural invariant and returns canonical JSON
// without applying output budgets. It is intentionally for bounded renderers;
// final transport must still use EncodeResult.
func MarshalResult(result Result) ([]byte, error) {
	if err := validateResultWithoutBudgets(result); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	return encoded, nil
}

// OutputCharacters returns the frozen model-visible character calculation.
// It deliberately shares validation's calculation rather than duplicating it
// in callers that need to fit a result before encoding it.
func OutputCharacters(result Result) int { return renderedOutputCharacters(result) }
