package model

import (
	"bytes"
	"math"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	// MaximumReferenceTableEntries bounds how many distinct targets one
	// enclosing definition contributes, so a generated or very long function
	// cannot dominate the reference section of an index.
	MaximumReferenceTableEntries = 64
	// MaximumReferenceTableBytes bounds the rendered table, which is what a
	// reference record carries in TargetName.
	MaximumReferenceTableBytes = 4096
	// MaximumReferenceTargetBytes bounds one referenced name.
	MaximumReferenceTargetBytes = 256
	// maximumReferenceTableNumber is the largest line or count a table entry
	// may carry, so the sum of the counts still fits the uint32 the index
	// format encodes.
	maximumReferenceTableNumber = math.MaxUint32
)

// ReferenceEntry is one referenced name inside a reference record: the name as
// it is written in the source, the line of its first occurrence, and how many
// occurrences merged into it.
type ReferenceEntry struct {
	Name  string
	Line  int
	Count int
}

// FormatReferenceTable renders entries as the target table a reference record
// carries in TargetName: "name:line:count" joined by ";". The caller owns the
// order and the bounds; ParseReferenceTable accepts exactly what this writes
// for entries that satisfy them.
func FormatReferenceTable(entries []ReferenceEntry) string {
	var builder strings.Builder
	for index, entry := range entries {
		if index != 0 {
			builder.WriteByte(';')
		}
		builder.WriteString(entry.Name)
		builder.WriteByte(':')
		builder.WriteString(strconv.Itoa(entry.Line))
		builder.WriteByte(':')
		builder.WriteString(strconv.Itoa(entry.Count))
	}
	return builder.String()
}

// ParseReferenceTable reads a target table. It reports false for anything the
// grammar does not accept, so a caller never sees a partially parsed table.
// Entry order and uniqueness are the writer's contract, not a validation rule:
// the reader keeps whatever order the table carries.
func ParseReferenceTable(table string) ([]ReferenceEntry, bool) {
	entries := make([]ReferenceEntry, 0, 8)
	_, _, ok := scanReferenceTable([]byte(table), func(entry ReferenceEntry) bool {
		entries = append(entries, entry)
		return true
	})
	if !ok {
		return nil, false
	}
	return entries, true
}

// ScanReferenceTable validates a target table without materializing it and
// reports the number of entries and the sum of their counts, which is what a
// reference record must carry in ReferenceCount.
func ScanReferenceTable(table []byte) (entries int, total uint64, ok bool) {
	return scanReferenceTable(table, nil)
}

// ValidReferenceTargetName reports whether a referenced name has a stable
// written form: names carrying the table separators, whitespace, or control
// characters have none and are never recorded.
func ValidReferenceTargetName(name string) bool {
	return validReferenceTargetName([]byte(name))
}

func scanReferenceTable(table []byte, collect func(ReferenceEntry) bool) (int, uint64, bool) {
	if len(table) == 0 || len(table) > MaximumReferenceTableBytes {
		return 0, 0, false
	}
	entries := 0
	total := uint64(0)
	for {
		field := table
		if separator := bytes.IndexByte(table, ';'); separator >= 0 {
			field, table = table[:separator], table[separator+1:]
			if len(table) == 0 {
				return 0, 0, false
			}
		} else {
			table = nil
		}
		entries++
		if entries > MaximumReferenceTableEntries {
			return 0, 0, false
		}
		name, line, count, ok := scanReferenceEntry(field)
		if !ok {
			return 0, 0, false
		}
		total += count
		if total > maximumReferenceTableNumber {
			return 0, 0, false
		}
		if collect != nil && !collect(ReferenceEntry{Name: string(name), Line: int(line), Count: int(count)}) {
			return 0, 0, false
		}
		if len(table) == 0 {
			return entries, total, true
		}
	}
}

func scanReferenceEntry(field []byte) (name []byte, line, count uint64, ok bool) {
	nameEnd := bytes.IndexByte(field, ':')
	if nameEnd < 0 {
		return nil, 0, 0, false
	}
	name, rest := field[:nameEnd], field[nameEnd+1:]
	lineEnd := bytes.IndexByte(rest, ':')
	if lineEnd < 0 {
		return nil, 0, 0, false
	}
	line, ok = referenceTableNumber(rest[:lineEnd])
	if !ok {
		return nil, 0, 0, false
	}
	count, ok = referenceTableNumber(rest[lineEnd+1:])
	if !ok || !validReferenceTargetName(name) {
		return nil, 0, 0, false
	}
	return name, line, count, true
}

// referenceTableNumber reads a positive decimal without a sign or a leading
// zero, so one table has exactly one written form.
func referenceTableNumber(value []byte) (uint64, bool) {
	if len(value) == 0 || len(value) > 10 || value[0] == '0' {
		return 0, false
	}
	number := uint64(0)
	for _, digit := range value {
		if digit < '0' || digit > '9' {
			return 0, false
		}
		number = number*10 + uint64(digit-'0')
	}
	if number > maximumReferenceTableNumber || number > uint64(math.MaxInt) {
		return 0, false
	}
	return number, true
}

func validReferenceTargetName(name []byte) bool {
	if len(name) == 0 || len(name) > MaximumReferenceTargetBytes || !utf8.Valid(name) {
		return false
	}
	for remaining := name; len(remaining) != 0; {
		character, size := utf8.DecodeRune(remaining)
		if character == ':' || character == ';' || unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
		remaining = remaining[size:]
	}
	return true
}
