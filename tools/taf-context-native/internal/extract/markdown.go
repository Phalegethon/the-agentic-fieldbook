package extract

import (
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
	text  string
	bytes int
}

type markdownHeading struct {
	qualified string
	start     int
	end       int
}

func (extractor markdownExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	if !utf8.Valid(file.Bytes) {
		return nil, Report{ParserVersion: markdownParserVersion, ParseFailures: 1, WarningCodes: []string{"markdown-invalid-utf8"}}
	}
	lines := markdownLines(string(file.Bytes))
	fenced := markdownFencedLines(lines)
	headings := findMarkdownHeadings(lines, fenced)
	records := make([]model.Record, 0, len(headings)*2+1)
	warnings := []string{}
	appendRecord := func(record model.Record) bool {
		if len(records) >= maximumMarkdownRecords {
			warnings = append(warnings, "markdown-record-limit")
			return false
		}
		records = append(records, record)
		return true
	}
	for _, heading := range headings {
		if !appendRecord(structuralRecord(heading.qualified, model.Heading, "document", heading.start, heading.end)) {
			break
		}
	}
	sections := markdownSections(file.RelativePath, lines, headings)
	for _, section := range sections {
		chunk := 1
		for _, prose := range markdownProseRanges(section, fenced) {
			for start := prose.start; start <= prose.end; {
				if lines[start-1].bytes > maximumMarkdownChunkBytes {
					warnings = append(warnings, "markdown-line-too-long")
					start++
					continue
				}
				end := markdownChunkEnd(lines, start, prose.end)
				qualified := section.qualified + "#chunk-" + decimal(chunk)
				record := structuralRecord(qualified, model.DocumentChunk, "document", start, end)
				record.SearchTerms = markdownChunkTerms(lines, start, end)
				if !appendRecord(record) {
					return records, Report{ParserVersion: markdownParserVersion, WarningCodes: warnings}
				}
				start = end + 1
				chunk++
			}
		}
	}
	return records, Report{ParserVersion: markdownParserVersion, WarningCodes: warnings}
}

func markdownLines(source string) []markdownLine {
	raw := strings.Split(source, "\n")
	if len(raw) != 0 && raw[len(raw)-1] == "" {
		raw = raw[:len(raw)-1]
	}
	lines := make([]markdownLine, len(raw))
	for index, line := range raw {
		lineBytes := len(line) + 1
		line = strings.TrimSuffix(line, "\r")
		lines[index] = markdownLine{text: line, bytes: lineBytes}
	}
	return lines
}

func markdownFencedLines(lines []markdownLine) []bool {
	fenced := make([]bool, len(lines))
	fenceCharacter := byte(0)
	fenceLength := 0
	for index, line := range lines {
		if fenceCharacter != 0 {
			fenced[index] = true
			if markdownFenceClose(line.text, fenceCharacter, fenceLength) {
				fenceCharacter, fenceLength = 0, 0
			}
			continue
		}
		if character, length, ok := markdownFenceOpen(line.text); ok {
			fenced[index] = true
			fenceCharacter, fenceLength = character, length
		}
	}
	return fenced
}

func findMarkdownHeadings(lines []markdownLine, fenced []bool) []markdownHeading {
	var headings []markdownHeading
	var hierarchy [6]string
	for index := 0; index < len(lines); index++ {
		line := lines[index].text
		start := index + 1
		if fenced[index] {
			continue
		}
		text, level, ok := markdownATXHeading(line)
		end := index + 1
		if !ok && index+1 < len(lines) && !fenced[index+1] {
			if setextLevel := markdownSetextLevel(lines[index+1].text); setextLevel != 0 {
				if setextText, textOK := markdownSetextText(line); textOK {
					text, level, ok, end = setextText, setextLevel, true, index+2
					index++
				}
			}
		}
		if !ok || text == "" {
			continue
		}
		for depth := level - 1; depth < len(hierarchy); depth++ {
			hierarchy[depth] = ""
		}
		hierarchy[level-1] = text
		parts := make([]string, 0, level)
		for depth := 0; depth < level; depth++ {
			if hierarchy[depth] != "" {
				parts = append(parts, hierarchy[depth])
			}
		}
		headings = append(headings, markdownHeading{qualified: strings.Join(parts, "."), start: start, end: end})
	}
	return headings
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
	if closing < len(text) && closing > 0 && (text[closing-1] == ' ' || text[closing-1] == '\t') {
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
	return rest, rest != ""
}

type markdownSection struct {
	qualified string
	start     int
	end       int
}

func markdownProseRanges(section markdownSection, fenced []bool) []markdownSection {
	var ranges []markdownSection
	for line := section.start; line <= section.end; {
		for line <= section.end && fenced[line-1] {
			line++
		}
		if line > section.end {
			break
		}
		start := line
		for line <= section.end && !fenced[line-1] {
			line++
		}
		ranges = append(ranges, markdownSection{qualified: section.qualified, start: start, end: line - 1})
	}
	return ranges
}

func markdownSections(relative string, lines []markdownLine, headings []markdownHeading) []markdownSection {
	if len(lines) == 0 {
		return nil
	}
	var sections []markdownSection
	if len(headings) == 0 || headings[0].start > 1 {
		end := len(lines)
		if len(headings) != 0 {
			end = headings[0].start - 1
		}
		sections = append(sections, markdownSection{qualified: markdownDocumentName(relative), start: 1, end: end})
	}
	for index, heading := range headings {
		end := len(lines)
		if index+1 < len(headings) {
			end = headings[index+1].start - 1
		}
		sections = append(sections, markdownSection{qualified: heading.qualified, start: heading.start, end: end})
	}
	return sections
}

func markdownDocumentName(relative string) string {
	withoutExtension := strings.TrimSuffix(relative, path.Ext(relative))
	return strings.ReplaceAll(withoutExtension, "/", ".")
}

func markdownChunkEnd(lines []markdownLine, start, maximum int) int {
	end := start - 1
	bytes := 0
	for end < maximum && end-start+1 < maximumMarkdownChunkLines {
		next := lines[end].bytes
		if end >= start && bytes+next > maximumMarkdownChunkBytes {
			break
		}
		bytes += next
		end++
	}
	if end < start {
		return start
	}
	return end
}

func markdownChunkTerms(lines []markdownLine, start, end int) []string {
	var terms []string
	for line := start; line <= end && len(terms) < 64; line++ {
		for _, term := range strings.FieldsFunc(lines[line-1].text, func(character rune) bool {
			return !unicodeLetterOrDigit(character)
		}) {
			if len(term) <= 128 {
				terms = append(terms, term)
				if len(terms) == 64 {
					break
				}
			}
		}
	}
	return terms
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
