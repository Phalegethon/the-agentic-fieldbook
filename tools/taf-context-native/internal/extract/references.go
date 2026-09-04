package extract

import (
	"sort"
	"strconv"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

// referenceScope is the definition a use belongs to: the qualified name of the
// enclosing definition exactly as its own record carries it, and that
// definition's line range. A use at file scope carries the module's name and
// the module's range.
type referenceScope struct {
	name  string
	start int
	end   int
}

// referenceCollector groups the uses of names inside one file by the
// definition that contains them, so a file contributes at most one reference
// record per definition rather than one per (definition, target) pair. The
// merged targets become that record's target table.
type referenceCollector struct {
	groups  []referenceGroup
	index   map[referenceScope]int
	limited bool
	skipped int
}

type referenceGroup struct {
	scope   referenceScope
	entries []model.ReferenceEntry
	targets map[string]int
}

// add merges one use of target, seen on line, into the group of scope. A
// target without a stable written name is counted as skipped instead.
func (collector *referenceCollector) add(scope referenceScope, target string, line int) {
	if !model.ValidReferenceTargetName(target) {
		collector.skipped++
		return
	}
	if scope.name == "" || scope.start < 1 || scope.end < scope.start || line < 1 {
		return
	}
	if collector.index == nil {
		collector.index = make(map[referenceScope]int)
	}
	// Two definitions may be written with the same qualified name in one file,
	// so the group is keyed by the definition's name and its range together:
	// each one keeps its own record instead of merging into the first.
	position, known := collector.index[scope]
	if !known {
		position = len(collector.groups)
		collector.index[scope] = position
		collector.groups = append(collector.groups, referenceGroup{scope: scope, targets: make(map[string]int)})
	}
	group := &collector.groups[position]
	if entry, merged := group.targets[target]; merged {
		if line < group.entries[entry].Line {
			group.entries[entry].Line = line
		}
		group.entries[entry].Count++
		return
	}
	// Every distinct target is collected here and both bounds are applied in
	// flush, after the entries are ordered, so a table drops the entries it
	// orders last rather than the ones the parser visited last. The number of
	// distinct targets is bounded by the source file the extractor accepted.
	group.targets[target] = len(group.entries)
	group.entries = append(group.entries, model.ReferenceEntry{Name: target, Line: line, Count: 1})
}

// flush renders one reference record per enclosing definition, in the order
// the definitions were first seen, together with the warning codes the file
// earned. Entries are ordered by the line they first appear on, so the table
// of a definition does not depend on the order the parser visited its calls,
// and both the entry cap and the byte bound cut that order's tail.
func (collector *referenceCollector) flush() ([]model.Record, []string) {
	records := make([]model.Record, 0, len(collector.groups))
	for index := range collector.groups {
		group := &collector.groups[index]
		sort.Slice(group.entries, func(left, right int) bool {
			if group.entries[left].Line != group.entries[right].Line {
				return group.entries[left].Line < group.entries[right].Line
			}
			return group.entries[left].Name < group.entries[right].Name
		})
		if len(group.entries) > model.MaximumReferenceTableEntries {
			group.entries = group.entries[:model.MaximumReferenceTableEntries]
			collector.limited = true
		}
		entries, total := collector.boundedEntries(group.entries)
		if len(entries) == 0 {
			continue
		}
		start, end := referenceRange(group.scope, entries)
		records = append(records, model.Record{
			StartLine:      start,
			EndLine:        end,
			RecordKind:     model.Reference,
			SourceType:     "source",
			QualifiedName:  group.scope.name,
			EvidenceClass:  model.Verified,
			TargetName:     model.FormatReferenceTable(entries),
			ReferenceCount: total,
		})
	}
	var warnings []string
	if collector.limited {
		warnings = append(warnings, "reference-limit")
	}
	if collector.skipped > 0 {
		warnings = append(warnings, "reference-skipped")
	}
	return records, warnings
}

// boundedEntries keeps the longest prefix of entries whose rendered table fits
// the format bound, and reports the sum of the counts it kept. Dropping the
// tail is a work limit, so the file reports incomplete extraction.
func (collector *referenceCollector) boundedEntries(entries []model.ReferenceEntry) ([]model.ReferenceEntry, int) {
	size, total := 0, 0
	for kept, entry := range entries {
		width := len(entry.Name) + len(strconv.Itoa(entry.Line)) + len(strconv.Itoa(entry.Count)) + 2
		if kept != 0 {
			width++
		}
		if size+width > model.MaximumReferenceTableBytes {
			collector.limited = true
			return entries[:kept], total
		}
		size += width
		total += entry.Count
	}
	return entries, total
}

// referenceRange is the range the record covers. It is the enclosing
// definition's own range, except where that range does not contain the uses
// the record carries: Go's module scope is the package clause alone, so a
// package-level initializer would sit outside it. There the entries' own
// first and last line describe the record honestly.
func referenceRange(scope referenceScope, entries []model.ReferenceEntry) (int, int) {
	first, last := entries[0].Line, entries[len(entries)-1].Line
	if first >= scope.start && last <= scope.end {
		return scope.start, scope.end
	}
	return first, last
}
