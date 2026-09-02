package query

import (
	"cmp"
	"slices"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// rankedCandidate carries every field used by the total ordering. Admission
// computes these fields once; ranking comparisons never rescan or normalize a
// record.
type rankedCandidate struct {
	record         model.Record
	tier           int
	kindClass      int
	evidence       int
	normalizedPath string
	startLine      int
	kind           string
	normalizedName string
	identity       string
	source         int
	mapKind        int
}

func newRankedCandidate(record model.Record, tier int) rankedCandidate {
	return rankedCandidate{
		record:         record,
		tier:           tier,
		kindClass:      kindClass(record.RecordKind),
		evidence:       evidenceTier(record),
		normalizedPath: normalize(record.Path),
		startLine:      record.StartLine,
		kind:           string(record.RecordKind),
		normalizedName: normalize(record.QualifiedName),
		identity:       record.Identity,
		source:         sourceTier(record.SourceType),
		mapKind:        mapKindTier(record.RecordKind),
	}
}

// newMapCandidate builds a ranking candidate for RepositoryMap. Map
// representatives are already chosen one per file by
// compareRepresentativeCandidate and mapKindTier, so ranking the
// representatives together must not regroup them by kind again: kindClass is
// zeroed here, leaving path order (via compareRankedCandidate's normalizedPath
// comparison) as the only thing that orders map candidates against each
// other.
func newMapCandidate(record model.Record) rankedCandidate {
	candidate := newRankedCandidate(record, 0)
	candidate.kindClass = 0
	return candidate
}

// kindClass orders record kinds within a tier: places where a name is
// defined or described come first, configuration keys and document chunks
// second, imports last.
func kindClass(kind model.RecordKind) int {
	switch kind {
	case model.Configuration, model.DocumentChunk:
		return 1
	case model.Import:
		return 2
	default:
		return 0
	}
}

func compareRankedCandidate(left, right rankedCandidate) int {
	comparisons := [...]int{
		cmp.Compare(left.tier, right.tier),
		cmp.Compare(left.kindClass, right.kindClass),
		cmp.Compare(left.evidence, right.evidence),
		cmp.Compare(left.normalizedPath, right.normalizedPath),
		cmp.Compare(left.startLine, right.startLine),
		cmp.Compare(left.kind, right.kind),
		cmp.Compare(left.normalizedName, right.normalizedName),
		cmp.Compare(left.identity, right.identity),
	}
	for _, comparison := range comparisons {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func compareRepresentativeCandidate(left, right rankedCandidate) int {
	comparisons := [...]int{
		cmp.Compare(left.evidence, right.evidence),
		cmp.Compare(left.source, right.source),
		cmp.Compare(left.mapKind, right.mapKind),
		cmp.Compare(left.startLine, right.startLine),
		cmp.Compare(left.normalizedName, right.normalizedName),
		cmp.Compare(left.identity, right.identity),
	}
	for _, comparison := range comparisons {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

// boundedRanking maintains a sorted top-K incrementally. Each offered
// candidate is charged once; the top-K insertion's comparisons are free.
type boundedRanking struct {
	items   []rankedCandidate
	total   int
	maximum int
	budget  *workBudget
}

func newBoundedRanking(maximum int, budget *workBudget) boundedRanking {
	return boundedRanking{items: make([]rankedCandidate, 0, min(max(0, maximum), 64)), maximum: max(0, maximum), budget: budget}
}

func (ranking *boundedRanking) offer(record model.Record, tier int) bool {
	if !ranking.budget.visitRecord() {
		return false
	}
	return ranking.offerCandidate(newRankedCandidate(record, tier))
}

func (ranking *boundedRanking) offerCandidate(candidate rankedCandidate) bool {
	low, high := 0, len(ranking.items)
	for low < high {
		middle := low + (high-low)/2
		if compareRankedCandidate(ranking.items[middle], candidate) < 0 {
			low = middle + 1
		} else {
			high = middle
		}
	}
	ranking.total++
	if ranking.maximum == 0 || low >= ranking.maximum {
		return true
	}
	if len(ranking.items) < ranking.maximum {
		ranking.items = append(ranking.items, rankedCandidate{})
		copy(ranking.items[low+1:], ranking.items[low:])
		ranking.items[low] = candidate
		return true
	}
	copy(ranking.items[low+1:], ranking.items[low:len(ranking.items)-1])
	ranking.items[low] = candidate
	return true
}

func (ranking *boundedRanking) records() ([]model.Record, int) {
	records := make([]model.Record, len(ranking.items))
	for index := range ranking.items {
		records[index] = ranking.items[index].record
	}
	return records, max(0, ranking.total-len(ranking.items))
}

func evidenceTier(record model.Record) int {
	if record.EvidenceClass == model.Verified {
		return 0
	}
	if record.EvidenceClass == model.Inferred {
		return 1
	}
	return 2
}

func matchesOperation(record model.Record, operation wire.Operation) bool {
	switch operation {
	case wire.SearchSymbols:
		return record.RecordKind != model.Heading && record.RecordKind != model.DocumentChunk && record.SourceType != "document"
	case wire.SearchDocs:
		return record.RecordKind == model.Heading || record.RecordKind == model.DocumentChunk || record.SourceType == "document"
	default:
		return true
	}
}

func matchesFilters(record model.Record, filters wire.Filters) bool {
	if len(filters.PathPrefixes) != 0 {
		matched := false
		for _, prefix := range filters.PathPrefixes {
			if strings.HasPrefix(record.Path, prefix) {
				matched = true
				break
			}
		}
		if !matched {
			return false
		}
	}
	return matchesOne(filters.Languages, record.Language) && matchesOne(filters.SymbolKinds, string(record.RecordKind)) && matchesOne(filters.SourceTypes, record.SourceType)
}

func matchesOne(values []string, actual string) bool {
	if len(values) == 0 {
		return true
	}
	return slices.Contains(values, actual)
}

func deref(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
