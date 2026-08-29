package extract

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"unicode"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const tomlParserVersion = "taf-toml@1"
const jsonParserVersion = "encoding/json@go1.27"
const maximumJSONDepth = 64
const maximumConfigurationCollectionItems = 64
const maximumConfigurationRecords = 4096
const maximumConfigurationKeyComponents = 64

type tomlExtractor struct{ extensions []string }

func (extractor tomlExtractor) Language() string      { return "toml" }
func (extractor tomlExtractor) ParserVersion() string { return tomlParserVersion }
func (extractor tomlExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor tomlExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}

func (extractor tomlExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	lines := strings.Split(string(file.Bytes), "\n")
	if len(lines) != 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	var records []model.Record
	var table []string
	continuation := tomlValueState{}
	for index, raw := range lines {
		lineNumber := index + 1
		if continuation.active() {
			if !continuation.scan(raw) {
				return nil, tomlFailure()
			}
			continue
		}
		line, ok := stripTOMLComment(raw)
		if !ok {
			return nil, tomlFailure()
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "[[") {
			if !strings.HasSuffix(line, "]]") || len(line) < 5 {
				return nil, tomlFailure()
			}
			parsed, ok := parseTOMLKeyPath(strings.TrimSpace(line[2 : len(line)-2]))
			if !ok {
				return nil, tomlFailure()
			}
			table = parsed
			if !appendConfigurationRecord(&records, strings.Join(table, "."), lineNumber) {
				return records, configurationLimitReport(tomlParserVersion, "toml-record-limit")
			}
			continue
		}
		if strings.HasPrefix(line, "[") {
			if !strings.HasSuffix(line, "]") || strings.HasPrefix(line, "[[") || len(line) < 3 {
				return nil, tomlFailure()
			}
			parsed, ok := parseTOMLKeyPath(strings.TrimSpace(line[1 : len(line)-1]))
			if !ok {
				return nil, tomlFailure()
			}
			table = parsed
			if !appendConfigurationRecord(&records, strings.Join(table, "."), lineNumber) {
				return records, configurationLimitReport(tomlParserVersion, "toml-record-limit")
			}
			continue
		}
		equals := findTOMLEquals(line)
		if equals < 0 {
			return nil, tomlFailure()
		}
		key, ok := parseTOMLKeyPath(strings.TrimSpace(line[:equals]))
		value := strings.TrimSpace(line[equals+1:])
		if !ok || value == "" {
			return nil, tomlFailure()
		}
		qualifiedParts := append(append([]string(nil), table...), key...)
		qualified := strings.Join(qualifiedParts, ".")
		if len(qualified) > 512 || !appendConfigurationRecord(&records, qualified, lineNumber) {
			return records, configurationLimitReport(tomlParserVersion, "toml-record-limit")
		}
		if !continuation.scan(value) {
			return nil, tomlFailure()
		}
	}
	if continuation.active() {
		return nil, tomlFailure()
	}
	return records, Report{ParserVersion: tomlParserVersion}
}

func tomlFailure() Report {
	return Report{ParserVersion: tomlParserVersion, ParseFailures: 1, WarningCodes: []string{"toml-parse-failure"}}
}

func appendConfigurationRecord(records *[]model.Record, qualified string, line int) bool {
	if qualified == "" || len(*records) >= maximumConfigurationRecords {
		return false
	}
	*records = append(*records, structuralRecord(qualified, model.Configuration, "configuration", line, line))
	return true
}

func configurationLimitReport(parserVersion, warning string) Report {
	return Report{ParserVersion: parserVersion, ParseFailures: 1, WarningCodes: []string{warning}}
}

func stripTOMLComment(line string) (string, bool) {
	quote := byte(0)
	escaped := false
	for index := 0; index < len(line); index++ {
		character := line[index]
		if quote == '"' && escaped {
			escaped = false
			continue
		}
		if quote == '"' && character == '\\' {
			escaped = true
			continue
		}
		if quote != 0 {
			if character == quote {
				quote = 0
			}
			continue
		}
		if character == '\'' || character == '"' {
			quote = character
			continue
		}
		if character == '#' {
			return line[:index], true
		}
	}
	return line, quote == 0 && !escaped
}

func findTOMLEquals(line string) int {
	quote := byte(0)
	escaped := false
	for index := 0; index < len(line); index++ {
		character := line[index]
		if quote == '"' && escaped {
			escaped = false
			continue
		}
		if quote == '"' && character == '\\' {
			escaped = true
			continue
		}
		if quote != 0 {
			if character == quote {
				quote = 0
			}
			continue
		}
		if character == '\'' || character == '"' {
			quote = character
			continue
		}
		if character == '=' {
			return index
		}
	}
	return -1
}

func parseTOMLKeyPath(value string) ([]string, bool) {
	var components []string
	for index := 0; ; {
		for index < len(value) && (value[index] == ' ' || value[index] == '\t') {
			index++
		}
		if index == len(value) || len(components) >= maximumConfigurationKeyComponents {
			return nil, false
		}
		var component string
		switch value[index] {
		case '\'', '"':
			quote := value[index]
			start := index
			index++
			escaped := false
			for index < len(value) {
				if quote == '"' && !escaped && value[index] == '\\' {
					escaped = true
					index++
					continue
				}
				if !escaped && value[index] == quote {
					break
				}
				escaped = false
				index++
			}
			if index == len(value) {
				return nil, false
			}
			literal := value[start : index+1]
			component = literal[1 : len(literal)-1]
			if quote == '"' && strings.ContainsRune(component, '\\') {
				// This deliberately narrow parser does not claim verified
				// support for TOML basic-key escapes.
				return nil, false
			}
			index++
		default:
			start := index
			for index < len(value) && (unicode.IsLetter(rune(value[index])) || unicode.IsDigit(rune(value[index])) || value[index] == '_' || value[index] == '-') {
				index++
			}
			if start == index {
				return nil, false
			}
			component = value[start:index]
		}
		if component == "" || len(component) > 128 {
			return nil, false
		}
		components = append(components, component)
		for index < len(value) && (value[index] == ' ' || value[index] == '\t') {
			index++
		}
		if index == len(value) {
			return components, true
		}
		if value[index] != '.' {
			return nil, false
		}
		index++
	}
}

type tomlValueState struct {
	multiline string
	square    int
	curly     int
}

func (state tomlValueState) active() bool {
	return state.multiline != "" || state.square != 0 || state.curly != 0
}

func (state *tomlValueState) scan(value string) bool {
	if state.multiline != "" {
		if strings.Contains(value, state.multiline) {
			state.multiline = ""
		}
		return true
	}
	quote := byte(0)
	escaped := false
	for index := 0; index < len(value); index++ {
		if quote == 0 && index+3 <= len(value) && (value[index:index+3] == `"""` || value[index:index+3] == `'''`) {
			delimiter := value[index : index+3]
			remainder := value[index+3:]
			if !strings.Contains(remainder, delimiter) {
				state.multiline = delimiter
				return true
			}
			index += 2 + strings.Index(remainder, delimiter) + 3
			continue
		}
		character := value[index]
		if quote == '"' && escaped {
			escaped = false
			continue
		}
		if quote == '"' && character == '\\' {
			escaped = true
			continue
		}
		if quote != 0 {
			if character == quote {
				quote = 0
			}
			continue
		}
		switch character {
		case '\'', '"':
			quote = character
		case '[':
			state.square++
		case ']':
			state.square--
		case '{':
			state.curly++
		case '}':
			state.curly--
		}
		if state.square < 0 || state.curly < 0 {
			return false
		}
	}
	return quote == 0 && !escaped
}

type jsonExtractor struct{ extensions []string }

func (extractor jsonExtractor) Language() string      { return "json" }
func (extractor jsonExtractor) ParserVersion() string { return jsonParserVersion }
func (extractor jsonExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor jsonExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}

var (
	errJSONDepth      = errors.New("JSON depth limit")
	errJSONCollection = errors.New("JSON collection limit")
	errJSONRecords    = errors.New("JSON record limit")
	errJSONDuplicate  = errors.New("duplicate JSON key")
)

type jsonParser struct {
	decoder *json.Decoder
	source  []byte
	records []model.Record
}

func (extractor jsonExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	decoder := json.NewDecoder(bytes.NewReader(file.Bytes))
	decoder.UseNumber()
	parser := jsonParser{decoder: decoder, source: file.Bytes}
	err := parser.value(nil, 0)
	if err == nil {
		_, trailingErr := decoder.Token()
		if !errors.Is(trailingErr, io.EOF) {
			err = errors.New("trailing JSON value")
		}
	}
	if err == nil {
		return parser.records, Report{ParserVersion: jsonParserVersion}
	}
	switch {
	case errors.Is(err, errJSONDepth):
		return parser.records, configurationLimitReport(jsonParserVersion, "json-depth-limit")
	case errors.Is(err, errJSONCollection):
		return parser.records, configurationLimitReport(jsonParserVersion, "json-collection-limit")
	case errors.Is(err, errJSONRecords):
		return parser.records, configurationLimitReport(jsonParserVersion, "json-record-limit")
	default:
		return nil, Report{ParserVersion: jsonParserVersion, ParseFailures: 1, WarningCodes: []string{"json-parse-failure"}}
	}
}

func (parser *jsonParser) value(keyPath []string, depth int) error {
	token, err := parser.decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	if depth >= maximumJSONDepth {
		return errJSONDepth
	}
	switch delimiter {
	case '{':
		return parser.object(keyPath, depth+1)
	case '[':
		return parser.array(arrayJSONPath(keyPath), depth+1)
	default:
		return errors.New("unexpected JSON delimiter")
	}
}

func (parser *jsonParser) object(parent []string, depth int) error {
	seen := make(map[string]bool)
	count := 0
	for parser.decoder.More() {
		if count >= maximumConfigurationCollectionItems {
			return errJSONCollection
		}
		before := parser.decoder.InputOffset()
		token, err := parser.decoder.Token()
		if err != nil {
			return err
		}
		key, ok := token.(string)
		if !ok || seen[key] {
			return errJSONDuplicate
		}
		seen[key] = true
		count++
		qualifiedPath := append(append([]string(nil), parent...), key)
		qualified := strings.Join(qualifiedPath, ".")
		if qualified == "" || len(qualified) > 512 || len(parser.records) >= maximumConfigurationRecords {
			return errJSONRecords
		}
		line := jsonTokenLine(parser.source, before, parser.decoder.InputOffset())
		parser.records = append(parser.records, structuralRecord(qualified, model.Configuration, "configuration", line, line))
		if err := parser.value(qualifiedPath, depth); err != nil {
			return err
		}
	}
	closing, err := parser.decoder.Token()
	if err != nil || closing != json.Delim('}') {
		return errors.New("unterminated JSON object")
	}
	return nil
}

func (parser *jsonParser) array(itemPath []string, depth int) error {
	count := 0
	for parser.decoder.More() {
		if count >= maximumConfigurationCollectionItems {
			return errJSONCollection
		}
		count++
		if err := parser.value(itemPath, depth); err != nil {
			return err
		}
	}
	closing, err := parser.decoder.Token()
	if err != nil || closing != json.Delim(']') {
		return errors.New("unterminated JSON array")
	}
	return nil
}

func arrayJSONPath(parent []string) []string {
	if len(parent) == 0 {
		return []string{"[]"}
	}
	result := append([]string(nil), parent...)
	result[len(result)-1] += "[]"
	return result
}

func jsonTokenLine(source []byte, before, after int64) int {
	start, end := int(before), int(after)
	if start < 0 {
		start = 0
	}
	if end > len(source) {
		end = len(source)
	}
	for start < end && source[start] != '"' {
		start++
	}
	return bytes.Count(source[:start], []byte{'\n'}) + 1
}
