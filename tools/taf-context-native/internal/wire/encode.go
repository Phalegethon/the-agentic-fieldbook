package wire

import (
	"encoding/json"
	"fmt"
	"io"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

func EncodeResult(writer io.Writer, result Result) error {
	if err := validateResult(result); err != nil {
		return err
	}
	for attempts := 0; attempts < 8; attempts++ {
		encoded, err := json.Marshal(result)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrInvalidWire, err)
		}
		count := utf8.RuneCount(encoded)
		if result.OutputCharacters == count {
			if count > 12000 || len(encoded)+1 > policy.ProductionV1.MaximumStdoutBytes {
				return ErrInvalidWire
			}
			_, err = writer.Write(append(encoded, '\n'))
			return err
		}
		result.OutputCharacters = count
	}
	return ErrInvalidWire
}
