package extract

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"strings"
	"unicode/utf8"

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
	state := newTOMLDocumentState()
	for index, raw := range lines {
		lineNumber := index + 1
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
			if !state.defineTable(parsed, true) || !state.appendRecord(strings.Join(parsed, "."), lineNumber) {
				return nil, tomlFailureOrLimit(state)
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
			if !state.defineTable(parsed, false) || !state.appendRecord(strings.Join(parsed, "."), lineNumber) {
				return nil, tomlFailureOrLimit(state)
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
		if _, supported := tomlValueKind(value); !supported {
			return nil, tomlFailure()
		}
		qualifiedParts := append(append([]string(nil), state.table...), key...)
		qualified := strings.Join(qualifiedParts, ".")
		if len(qualified) > 512 || !state.defineValue(key) || !state.appendRecord(qualified, lineNumber) {
			return nil, tomlFailureOrLimit(state)
		}
	}
	return state.records, Report{ParserVersion: tomlParserVersion}
}

func tomlFailure() Report {
	return Report{ParserVersion: tomlParserVersion, ParseFailures: 1, WarningCodes: []string{"toml-parse-failure"}}
}

func tomlFailureOrLimit(state *tomlDocumentState) Report {
	if state.recordLimited {
		return configurationLimitReport(tomlParserVersion, "toml-record-limit")
	}
	return tomlFailure()
}

type tomlNodeKind uint8

const (
	tomlImplicitTable tomlNodeKind = iota + 1
	tomlImplicitKeyTable
	tomlRegularTable
	tomlArrayTable
	tomlValue
)

type tomlDocumentState struct {
	records       []model.Record
	nodes         map[string]tomlNodeKind
	arrayCounts   map[string]int
	arrayValues   map[string]tomlNodeKind
	table         []string
	arrayScope    string
	recordLimited bool
}

func newTOMLDocumentState() *tomlDocumentState {
	return &tomlDocumentState{
		nodes:       make(map[string]tomlNodeKind),
		arrayCounts: make(map[string]int),
		arrayValues: make(map[string]tomlNodeKind),
	}
}

func (state *tomlDocumentState) appendRecord(qualified string, line int) bool {
	if qualified == "" || len(state.records) >= maximumConfigurationRecords {
		state.recordLimited = true
		return false
	}
	state.records = append(state.records, structuralRecord(qualified, model.Configuration, "configuration", line, line))
	return true
}

func (state *tomlDocumentState) defineTable(parts []string, array bool) bool {
	for length := 1; length < len(parts); length++ {
		prefix := tomlPathIdentity(parts[:length])
		switch state.nodes[prefix] {
		case 0:
			state.nodes[prefix] = tomlImplicitTable
		case tomlValue, tomlArrayTable:
			return false
		}
	}
	identity := tomlPathIdentity(parts)
	existing := state.nodes[identity]
	if array {
		if existing != 0 && existing != tomlArrayTable {
			return false
		}
		state.nodes[identity] = tomlArrayTable
		state.arrayCounts[identity]++
		state.arrayScope = identity + "#" + strconv.Itoa(state.arrayCounts[identity])
	} else {
		if existing != 0 && existing != tomlImplicitTable {
			return false
		}
		state.nodes[identity] = tomlRegularTable
		state.arrayScope = ""
	}
	state.table = append(state.table[:0], parts...)
	return true
}

func (state *tomlDocumentState) defineValue(key []string) bool {
	if state.arrayScope != "" {
		for length := 1; length < len(key); length++ {
			identity := state.arrayScope + "\x00" + tomlPathIdentity(key[:length])
			switch state.arrayValues[identity] {
			case 0:
				state.arrayValues[identity] = tomlImplicitKeyTable
			case tomlValue:
				return false
			}
		}
		identity := state.arrayScope + "\x00" + tomlPathIdentity(key)
		if state.arrayValues[identity] != 0 {
			return false
		}
		state.arrayValues[identity] = tomlValue
		return true
	}
	parts := append(append([]string(nil), state.table...), key...)
	for length := 1; length < len(parts); length++ {
		prefix := tomlPathIdentity(parts[:length])
		switch state.nodes[prefix] {
		case 0:
			state.nodes[prefix] = tomlImplicitKeyTable
		case tomlValue:
			return false
		}
	}
	identity := tomlPathIdentity(parts)
	if state.nodes[identity] != 0 {
		return false
	}
	state.nodes[identity] = tomlValue
	return true
}

func tomlPathIdentity(parts []string) string {
	return strings.Join(parts, "\x00")
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
			for index < len(value) && isTOMLBareKeyByte(value[index]) {
				index++
			}
			if start == index {
				return nil, false
			}
			component = value[start:index]
		}
		if component == "" || len(component) > 128 || !validTOMLKeyComponent(component) {
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

func isTOMLBareKeyByte(character byte) bool {
	return (character >= 'A' && character <= 'Z') || (character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') || character == '_' || character == '-'
}

func validTOMLKeyComponent(component string) bool {
	if !utf8.ValidString(component) {
		return false
	}
	for _, character := range component {
		if character < 0x20 || character == 0x7f {
			return false
		}
	}
	return true
}

func tomlValueKind(value string) (string, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", false
	}
	if value[0] == '[' {
		return tomlArrayKind(value)
	}
	return tomlPrimitiveKind(value)
}

func tomlPrimitiveKind(value string) (string, bool) {
	if value == "true" || value == "false" {
		return "boolean", true
	}
	if validTOMLSimpleString(value) {
		return "string", true
	}
	if kind, ok := validTOMLNumber(value); ok {
		return kind, true
	}
	return "", false
}

func validTOMLSimpleString(value string) bool {
	if len(value) < 2 || (value[0] != '"' && value[0] != '\'') || value[len(value)-1] != value[0] || strings.HasPrefix(value, `"""`) || strings.HasPrefix(value, `'''`) {
		return false
	}
	quote := value[0]
	for index := 1; index < len(value)-1; index++ {
		character := value[index]
		if character == quote || character == '\\' || character < 0x20 || character == 0x7f {
			return false
		}
	}
	return true
}

func tomlArrayKind(value string) (string, bool) {
	if len(value) < 2 || value[len(value)-1] != ']' {
		return "", false
	}
	contents := strings.TrimSpace(value[1 : len(value)-1])
	if contents == "" {
		return "array", true
	}
	var elements []string
	start := 0
	quote := byte(0)
	for index := 0; index < len(contents); index++ {
		character := contents[index]
		if quote != 0 {
			if character == '\\' || character < 0x20 || character == 0x7f {
				return "", false
			}
			if character == quote {
				quote = 0
			}
			continue
		}
		switch character {
		case '\'', '"':
			quote = character
		case ',', ']':
			if character == ']' {
				return "", false
			}
			elements = append(elements, strings.TrimSpace(contents[start:index]))
			start = index + 1
		case '[', '{', '}':
			return "", false
		}
	}
	if quote != 0 {
		return "", false
	}
	elements = append(elements, strings.TrimSpace(contents[start:]))
	wantedKind := ""
	for index, element := range elements {
		if element == "" {
			if index == len(elements)-1 {
				continue
			}
			return "", false
		}
		kind, ok := tomlPrimitiveKind(element)
		if !ok || (wantedKind != "" && kind != wantedKind) {
			return "", false
		}
		wantedKind = kind
	}
	if wantedKind == "" {
		return "", false
	}
	return "array-" + wantedKind, true
}

func validTOMLNumber(value string) (string, bool) {
	index := 0
	if value[index] == '+' || value[index] == '-' {
		index++
		if index == len(value) {
			return "", false
		}
	}
	integerStart := index
	if !consumeTOMLDigits(value, &index) {
		return "", false
	}
	integerDigits := strings.ReplaceAll(value[integerStart:index], "_", "")
	if len(integerDigits) > 1 && integerDigits[0] == '0' {
		return "", false
	}
	kind := "integer"
	if index < len(value) && value[index] == '.' {
		kind = "float"
		index++
		if !consumeTOMLDigits(value, &index) {
			return "", false
		}
	}
	if index < len(value) && (value[index] == 'e' || value[index] == 'E') {
		kind = "float"
		index++
		if index < len(value) && (value[index] == '+' || value[index] == '-') {
			index++
		}
		if !consumeTOMLDigits(value, &index) {
			return "", false
		}
	}
	if index != len(value) {
		return "", false
	}
	clean := strings.ReplaceAll(value, "_", "")
	if kind == "integer" {
		_, err := strconv.ParseInt(clean, 10, 64)
		return kind, err == nil
	}
	_, err := strconv.ParseFloat(clean, 64)
	return kind, err == nil
}

func consumeTOMLDigits(value string, index *int) bool {
	start := *index
	previousDigit := false
	for *index < len(value) {
		character := value[*index]
		if character >= '0' && character <= '9' {
			previousDigit = true
			*index++
			continue
		}
		if character == '_' && previousDigit && *index+1 < len(value) && value[*index+1] >= '0' && value[*index+1] <= '9' {
			previousDigit = false
			*index++
			continue
		}
		break
	}
	return *index > start && previousDigit
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
