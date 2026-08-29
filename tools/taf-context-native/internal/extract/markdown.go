package extract

import (
	"bytes"
	"path"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const markdownParserVersion = "taf-markdown@1"
const maximumMarkdownChunkLines = 200
const maximumMarkdownChunkBytes = 16 << 10
const maximumMarkdownRecords = 4096

type markdownExtractor struct {
	extensions []string
}

func (extractor markdownExtractor) Language() string      { return "markdown" }
func (extractor markdownExtractor) ParserVersion() string { return markdownParserVersion }
func (extractor markdownExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor markdownExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumMarkdownFileBytes)
}

type markdownLine struct {
	text   string
	bytes  int
	number int
	next   int
}

type markdownParser struct {
	source       []byte
	records      []model.Record
	warnings     []string
	hierarchy    [6]string
	documentName string
	section      string
	chunk        int
	chunkStart   int
	chunkEnd     int
	chunkLines   int
	chunkBytes   int
	chunkTerms   []string
	limited      bool
	failed       bool
}

func (extractor markdownExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	if !utf8.Valid(file.Bytes) {
		return nil, Report{ParserVersion: markdownParserVersion, ParseFailures: 1, WarningCodes: []string{"markdown-invalid-utf8"}}
	}
	parser := markdownParser{
		source:       file.Bytes,
		records:      make([]model.Record, 0, 256),
		documentName: markdownDocumentName(file.RelativePath),
	}
	parser.section = parser.documentName
	parser.chunk = 1
	parser.parse()
	report := Report{ParserVersion: markdownParserVersion, WarningCodes: parser.warnings}
	if parser.failed || parser.limited {
		report.ParseFailures = 1
	}
	return parser.records, report
}

func (parser *markdownParser) parse() {
	offset, lineNumber := 0, 1
	fenceCharacter := byte(0)
	fenceLength := 0
	for offset < len(parser.source) && !parser.limited {
		line, ok := nextMarkdownLine(parser.source, offset, lineNumber)
		if !ok {
			break
		}
		if fenceCharacter != 0 {
			if markdownFenceClose(line.text, fenceCharacter, fenceLength) {
				fenceCharacter, fenceLength = 0, 0
			}
			offset, lineNumber = line.next, line.number+1
			continue
		}
		if character, length, open := markdownFenceOpen(line.text); open {
			parser.flushChunk()
			fenceCharacter, fenceLength = character, length
			offset, lineNumber = line.next, line.number+1
			continue
		}
		if text, level, heading := markdownATXHeading(line.text); heading {
			parser.startHeading(text, level, line.number, line.number)
			parser.addChunkLine(line)
			offset, lineNumber = line.next, line.number+1
			continue
		}
		next, hasNext := nextMarkdownLine(parser.source, line.next, line.number+1)
		if hasNext {
			level := markdownSetextLevel(next.text)
			if text, supported := markdownSetextText(line.text); level != 0 && supported {
				parser.startHeading(text, level, line.number, next.number)
				parser.addChunkLine(line)
				parser.addChunkLine(next)
				offset, lineNumber = next.next, next.number+1
				continue
			}
		}
		parser.addChunkLine(line)
		offset, lineNumber = line.next, line.number+1
	}
	if fenceCharacter != 0 && !parser.limited {
		parser.warnings = append(parser.warnings, "markdown-unterminated-fence")
		parser.failed = true
	}
	parser.flushChunk()
}

func nextMarkdownLine(source []byte, offset, number int) (markdownLine, bool) {
	if offset < 0 || offset >= len(source) {
		return markdownLine{}, false
	}
	relativeEnd := bytes.IndexByte(source[offset:], '\n')
	end, next := len(source), len(source)
	if relativeEnd >= 0 {
		end = offset + relativeEnd
		next = end + 1
	}
	raw := source[offset:end]
	if len(raw) != 0 && raw[len(raw)-1] == '\r' {
		raw = raw[:len(raw)-1]
	}
	return markdownLine{text: string(raw), bytes: next - offset, number: number, next: next}, true
}

func (parser *markdownParser) startHeading(text string, level, start, end int) {
	parser.flushChunk()
	for depth := level - 1; depth < len(parser.hierarchy); depth++ {
		parser.hierarchy[depth] = ""
	}
	if len(text) > 512 {
		parser.warnings = append(parser.warnings, "markdown-heading-limit")
		parser.limited = true
		return
	}
	parser.hierarchy[level-1] = text
	parts := make([]string, 0, level)
	for depth := 0; depth < level; depth++ {
		if parser.hierarchy[depth] != "" {
			parts = append(parts, parser.hierarchy[depth])
		}
	}
	qualified := strings.Join(parts, ".")
	if len(qualified) > 512 {
		parser.warnings = append(parser.warnings, "markdown-heading-limit")
		parser.limited = true
		return
	}
	if qualified == "" {
		qualified = parser.documentName
	}
	parser.section = qualified
	parser.chunk = 1
	if text != "" {
		parser.appendRecord(structuralRecord(qualified, model.Heading, "document", start, end))
	}
}

func (parser *markdownParser) addChunkLine(line markdownLine) {
	if parser.limited {
		return
	}
	if line.bytes > maximumMarkdownChunkBytes {
		parser.flushChunk()
		parser.warnings = append(parser.warnings, "markdown-line-too-long")
		return
	}
	if parser.chunkLines != 0 && (parser.chunkLines == maximumMarkdownChunkLines || parser.chunkBytes+line.bytes > maximumMarkdownChunkBytes) {
		parser.flushChunk()
	}
	if parser.chunkLines == 0 {
		parser.chunkStart = line.number
	}
	parser.chunkEnd = line.number
	parser.chunkLines++
	parser.chunkBytes += line.bytes
	parser.appendChunkTerms(line.text)
}

func (parser *markdownParser) appendChunkTerms(line string) {
	if len(parser.chunkTerms) >= 64 {
		return
	}
	for _, term := range strings.FieldsFunc(line, func(character rune) bool {
		return !unicodeLetterOrDigit(character)
	}) {
		if len(term) <= 128 {
			parser.chunkTerms = append(parser.chunkTerms, term)
			if len(parser.chunkTerms) == 64 {
				return
			}
		}
	}
}

func (parser *markdownParser) flushChunk() {
	if parser.chunkLines == 0 || parser.limited {
		parser.resetChunk()
		return
	}
	record := structuralRecord(parser.section+"#chunk-"+decimal(parser.chunk), model.DocumentChunk, "document", parser.chunkStart, parser.chunkEnd)
	record.SearchTerms = append([]string(nil), parser.chunkTerms...)
	if parser.appendRecord(record) {
		parser.chunk++
	}
	parser.resetChunk()
}

func (parser *markdownParser) resetChunk() {
	parser.chunkStart = 0
	parser.chunkEnd = 0
	parser.chunkLines = 0
	parser.chunkBytes = 0
	parser.chunkTerms = parser.chunkTerms[:0]
}

func (parser *markdownParser) appendRecord(record model.Record) bool {
	if len(parser.records) >= maximumMarkdownRecords {
		parser.warnings = append(parser.warnings, "markdown-record-limit")
		parser.limited = true
		return false
	}
	parser.records = append(parser.records, record)
	return true
}

func markdownFenceOpen(line string) (byte, int, bool) {
	rest, ok := markdownIndentedContent(line)
	if !ok || len(rest) < 3 || (rest[0] != '`' && rest[0] != '~') {
		return 0, 0, false
	}
	character := rest[0]
	length := repeatedPrefix(rest, character)
	if length < 3 || (character == '`' && strings.ContainsRune(rest[length:], '`')) {
		return 0, 0, false
	}
	return character, length, true
}

func markdownFenceClose(line string, character byte, minimum int) bool {
	rest, ok := markdownIndentedContent(line)
	if !ok || repeatedPrefix(rest, character) < minimum {
		return false
	}
	return strings.TrimSpace(rest[repeatedPrefix(rest, character):]) == ""
}

func markdownIndentedContent(line string) (string, bool) {
	spaces := 0
	for spaces < len(line) && line[spaces] == ' ' {
		spaces++
	}
	if spaces > 3 || (spaces < len(line) && line[spaces] == '\t') {
		return "", false
	}
	return line[spaces:], true
}

func repeatedPrefix(value string, character byte) int {
	count := 0
	for count < len(value) && value[count] == character {
		count++
	}
	return count
}

func markdownATXHeading(line string) (string, int, bool) {
	rest, ok := markdownIndentedContent(line)
	if !ok {
		return "", 0, false
	}
	level := repeatedPrefix(rest, '#')
	if level == 0 || level > 6 || (len(rest) > level && rest[level] != ' ' && rest[level] != '\t') {
		return "", 0, false
	}
	text := strings.TrimSpace(rest[level:])
	closing := len(text)
	for closing > 0 && text[closing-1] == '#' {
		closing--
	}
	if closing < len(text) && (closing == 0 || text[closing-1] == ' ' || text[closing-1] == '\t') {
		text = strings.TrimSpace(text[:closing])
	}
	return text, level, true
}

func markdownSetextLevel(line string) int {
	rest, ok := markdownIndentedContent(line)
	if !ok {
		return 0
	}
	rest = strings.TrimSpace(rest)
	if len(rest) == 0 {
		return 0
	}
	character := rest[0]
	if character != '=' && character != '-' {
		return 0
	}
	for index := 1; index < len(rest); index++ {
		if rest[index] != character {
			return 0
		}
	}
	if character == '=' {
		return 1
	}
	return 2
}

func markdownSetextText(line string) (string, bool) {
	rest, ok := markdownIndentedContent(line)
	if !ok {
		return "", false
	}
	rest = strings.TrimSpace(rest)
	if rest == "" || markdownBlockStart(rest) {
		return "", false
	}
	first, _ := utf8.DecodeRuneInString(rest)
	if !unicode.IsLetter(first) && !unicode.IsDigit(first) {
		return "", false
	}
	return rest, true
}

func markdownBlockStart(line string) bool {
	if line[0] == '>' || markdownThematicBreak(line) {
		return true
	}
	if len(line) >= 2 && (line[0] == '-' || line[0] == '+' || line[0] == '*') && (line[1] == ' ' || line[1] == '\t') {
		return true
	}
	digits := 0
	for digits < len(line) && digits < 10 && line[digits] >= '0' && line[digits] <= '9' {
		digits++
	}
	return digits > 0 && digits <= 9 && digits+1 < len(line) && (line[digits] == '.' || line[digits] == ')') && (line[digits+1] == ' ' || line[digits+1] == '\t')
}

func markdownThematicBreak(line string) bool {
	marker := byte(0)
	count := 0
	for index := 0; index < len(line); index++ {
		if line[index] == ' ' || line[index] == '\t' {
			continue
		}
		if marker == 0 {
			if line[index] != '*' && line[index] != '-' && line[index] != '_' {
				return false
			}
			marker = line[index]
		}
		if line[index] != marker {
			return false
		}
		count++
	}
	return count >= 3
}

func markdownDocumentName(relative string) string {
	withoutExtension := strings.TrimSuffix(relative, path.Ext(relative))
	if withoutExtension == "" {
		withoutExtension = relative
	}
	return strings.ReplaceAll(withoutExtension, "/", ".")
}

func unicodeLetterOrDigit(character rune) bool {
	return character == '_' || unicode.IsLetter(character) || unicode.IsDigit(character)
}

func decimal(value int) string {
	if value == 0 {
		return "0"
	}
	var buffer [20]byte
	index := len(buffer)
	for value > 0 {
		index--
		buffer[index] = byte('0' + value%10)
		value /= 10
	}
	return string(buffer[index:])
}
