package query

import (
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// Changed answers which indexed symbols a change set touched. For every
// changed path it reads that path's slice of the canonical path index and
// admits the definition, entry-point, and module records whose line span
// intersects a changed hunk of the same path; an entry with no spans is a
// whole-file change and admits every such record of the path. Nothing is
// resolved and no file is reopened: the change set only selects records the
// index already carries.
func Changed(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits, len(snapshot.Records))
	if budget.maximum < 1 || request.ChangedRanges == nil {
		return budget.response([]model.Record{}, 0, false)
	}
	predicate := newFilterPredicate(request)
	ranking := newBoundedRanking(request.MaximumResults, budget)
	ordinals := snapshot.Query.PathOrdinals()
	partial, unindexed := false, false
	for _, entry := range *request.ChangedRanges {
		// Two raw paths can share one normalized path, so the range the binary
		// search returns is still filtered by the exact path of every record.
		start, end := relatedPathRange(snapshot.Records, ordinals, entry.Path)
		indexed := false
		for index := start; index < end; index++ {
			if !budget.visitRecord() {
				partial = true
				break
			}
			ordinal := ordinals[index]
			if uint64(ordinal) >= uint64(len(snapshot.Records)) {
				budget.exhausted = true
				partial = true
				break
			}
			record := snapshot.Records[ordinal]
			if record.Path != entry.Path {
				continue
			}
			// A path the index carries any record for is an indexed path, even
			// when none of its records is a symbol a change set may return.
			indexed = true
			if !changedSymbolKind(record.RecordKind) || !intersectsChange(record, entry.Ranges) {
				continue
			}
			if !predicate.permits(record) {
				continue
			}
			if !ranking.offer(record, 0) {
				partial = true
				break
			}
		}
		// A scan the budget cut short proves nothing about the path, so only a
		// completely scanned path with no record at all is reported.
		if !indexed && !partial {
			unindexed = true
		}
		if partial {
			break
		}
	}
	selected, omitted := ranking.records()
	response := budget.response(selected, omitted, partial)
	response.Unindexed = unindexed
	return response
}

// changedSymbolKind names the record kinds a change set may return: the places
// a name is defined, the entry points, and the module record that stands for
// the file's package. An import or a use of a name describes no symbol of its
// own, and document records carry prose rather than symbols.
func changedSymbolKind(kind model.RecordKind) bool {
	return definitionRecord(kind) || kind == model.Module
}

// intersectsChange reports whether a record's inclusive line span meets any
// changed span of its path. An empty span list is a whole-file change.
func intersectsChange(record model.Record, spans [][2]int) bool {
	if len(spans) == 0 {
		return true
	}
	for _, span := range spans {
		if record.StartLine <= span[1] && record.EndLine >= span[0] {
			return true
		}
	}
	return false
}
