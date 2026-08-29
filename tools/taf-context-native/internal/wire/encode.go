package wire

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

func EncodeResult(writer io.Writer, result Result) error {
	if err := validateResult(result); err != nil {
		return err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidWire, err)
	}
	if len(encoded)+1 > policy.ProductionLimits().MaximumStdoutBytes {
		return ErrInvalidWire
	}
	_, err = writer.Write(append(encoded, '\n'))
	return err
}

// OutputCharacters returns the frozen model-visible character calculation.
// It deliberately shares validation's calculation rather than duplicating it
// in callers that need to fit a result before encoding it.
func OutputCharacters(result Result) int { return renderedOutputCharacters(result) }
