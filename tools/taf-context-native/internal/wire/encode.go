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
