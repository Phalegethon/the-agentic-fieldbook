package extract

import (
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

const (
	maximumPreviewRunes     = 160
	maximumHeadingBodyLines = 20
	previewEllipsis         = '…'
)

// previewFor derives the one-line display hint stored with a record. It is a
// deterministic function of the file bytes and the record's line range, is
// excluded from record identity, and is never evidence: source-snippets stays
// the verified path.
func previewFor(record model.Record, source []byte, lineStarts []int) string {
	switch record.RecordKind {
	case model.Definition:
		return sanitizePreview(lineText(source, lineStarts, record.StartLine))
	case model.Heading:
		return sanitizePreview(headingBodyLine(source, lineStarts, record.EndLine))
	default:
		return ""
	}
}

// lineText returns line number (1-based) without its line terminator.
func lineText(source []byte, lineStarts []int, number int) string {
	if number < 1 || number > len(lineStarts) {
		return ""
	}
	start := lineStarts[number-1]
	end := len(source)
	if number < len(lineStarts) {
		end = lineStarts[number]
	}
	return strings.TrimRight(string(source[start:end]), "\r\n")
}

// headingBodyLine is the first non-blank line after the heading that is not
// an ATX or setext heading, a fence delimiter, or a thematic break, within a
// bounded window. The next heading ends the search.
func headingBodyLine(source []byte, lineStarts []int, headingEnd int) string {
	for number := headingEnd + 1; number <= headingEnd+maximumHeadingBodyLines && number <= len(lineStarts); number++ {
		raw := lineText(source, lineStarts, number)
		text := strings.TrimSpace(raw)
		if text == "" {
			continue
		}
		if _, _, heading := markdownATXHeading(text); heading {
			return ""
		}
		if _, _, fence := markdownFenceOpen(text); fence {
			continue
		}
		if number+1 <= len(lineStarts) {
			next := lineText(source, lineStarts, number+1)
			if markdownSetextLevel(next) != 0 {
				if _, supported := markdownSetextText(raw); supported {
					return ""
				}
			}
		}
		if markdownThematicBreak(text) || markdownSetextLevel(text) != 0 {
			continue
		}
		return text
	}
	return ""
}

// sanitizePreview replaces control characters and tabs with spaces, collapses
// whitespace runs, trims, and cuts to maximumPreviewRunes code points ending
// with an ellipsis. Invalid UTF-8 yields an empty preview.
func sanitizePreview(text string) string {
	if !utf8.ValidString(text) {
		return ""
	}
	var builder strings.Builder
	pendingSpace := false
	for _, runeValue := range text {
		if unicode.IsControl(runeValue) || unicode.IsSpace(runeValue) {
			pendingSpace = builder.Len() > 0
			continue
		}
		if pendingSpace {
			builder.WriteByte(' ')
			pendingSpace = false
		}
		builder.WriteRune(runeValue)
	}
	output := builder.String()
	if utf8.RuneCountInString(output) <= maximumPreviewRunes {
		return output
	}
	runes := []rune(output)
	return string(runes[:maximumPreviewRunes-1]) + string(previewEllipsis)
}
