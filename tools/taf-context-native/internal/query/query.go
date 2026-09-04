// Package query selects bounded, deterministic evidence from an immutable store snapshot.
package query

import (
	"cmp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// Response carries the ranked records, the omissions the ranking counted, and
// Partial, which is true only when a budget stopped the search before it
// examined everything it needed to. Omitted > 0 alone never sets Partial.
type Response struct {
	Records    []model.Record
	Omitted    int
	Partial    bool
	Counters   model.WorkCounters
	TermVisits int
	// Unindexed is set by Changed alone: it reports that at least one changed
	// path carried no record at all, which the caller turns into one warning
	// rather than into findings or omissions.
	Unindexed bool
}

// Match tiers, lower is better. Fuzzy tiers add the edit distance.
const (
	tierExactName  = 0
	tierExactToken = 1
	tierPrefix     = 2
	tierSubstring  = 3
	tierFuzzyBase  = 4
)

const (
	recordBudgetMultiplier = 4
	minimumFuzzyRunes      = 4
)

// workBudget charges one unit per posting entry visited and per candidate
// offered to the ranking; dictionary terms are charged to their own ceiling.
// Binary searches, map lookups, and ranking comparisons are free because the
// work already charged bounds them.
type workBudget struct {
	records      int
	maximum      int
	terms        int
	maximumTerms int
	exhausted    bool
}

func newWorkBudget(limits policy.Limits, recordCount int) *workBudget {
	maximum := max(0, limits.MaximumLexicalCandidates)
	if scaled := recordCount * recordBudgetMultiplier; scaled > maximum {
		maximum = scaled
	}
	return &workBudget{maximum: maximum, maximumTerms: max(0, limits.MaximumDictionaryTerms)}
}

func (budget *workBudget) visitRecord() bool {
	if budget.records >= budget.maximum {
		budget.exhausted = true
		return false
	}
	budget.records++
	return true
}

func (budget *workBudget) visitTerm() bool {
	if budget.terms >= budget.maximumTerms {
		budget.exhausted = true
		return false
	}
	budget.terms++
	return true
}

func (budget *workBudget) response(records []model.Record, omitted int, partial bool) Response {
	return Response{
		Records:    records,
		Omitted:    omitted,
		Partial:    partial || budget.exhausted,
		Counters:   model.WorkCounters{ConsideredRecords: budget.records},
		TermVisits: budget.terms,
	}
}

// Search admits candidates from the exact-name postings and from the sorted
// token dictionary, intersects the per-word candidate sets, applies the
// request filters as record predicates, and ranks the survivors.
func Search(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits, len(snapshot.Records))
	queryText := normalize(deref(request.Query))
	// An empty query or an empty snapshot examines nothing and is not exhausted.
	if queryText == "" || budget.maximum < 1 {
		return budget.response([]model.Record{}, 0, false)
	}
	collector := candidateCollector{
		snapshot:  snapshot,
		budget:    budget,
		predicate: newFilterPredicate(request),
		seen:      make(map[uint32]struct{}, 64),
		ranking:   newBoundedRanking(request.MaximumResults, budget),
	}
	collector.admitPosting(snapshot.Query.QualifiedOrdinals(queryText), tierExactName)
	collector.admitPosting(snapshot.Query.ShortOrdinals(queryText), tierExactName)

	words := strings.Fields(queryText)
	matches := make([][]taggedOrdinal, 0, len(words))
	for _, word := range words {
		matches = append(matches, collector.matchWord(word, limits.MaximumFuzzyDistance, max(0, limits.MaximumTermsPerWord)))
	}
	for _, candidate := range intersectWords(matches) {
		collector.admit(candidate.ordinal, candidate.tier)
	}
	selected, omitted := collector.ranking.records()
	return budget.response(selected, omitted, collector.partial)
}

type taggedOrdinal struct {
	ordinal uint32
	tier    int
}

type candidateCollector struct {
	snapshot  store.Snapshot
	budget    *workBudget
	predicate filterPredicate
	seen      map[uint32]struct{}
	ranking   boundedRanking
	partial   bool
}

func (collector *candidateCollector) admitPosting(ordinals []uint32, tier int) {
	for _, ordinal := range ordinals {
		// A reference record is keyed by the name it uses, so it sits in the
		// very postings a search for that name reads, and no search operation
		// may return one. Skipping it before the budget is charged keeps the
		// work a search does - and the counters that report it - the work of
		// the records the search can actually return.
		if uint64(ordinal) < uint64(len(collector.snapshot.Records)) && collector.snapshot.Records[ordinal].RecordKind == model.Reference {
			continue
		}
		if !collector.budget.visitRecord() {
			collector.partial = true
			return
		}
		collector.admit(ordinal, tier)
	}
}

// admit offers one record to the ranking at most once. Filters are record
// predicates evaluated here; they never consult facet postings.
func (collector *candidateCollector) admit(ordinal uint32, tier int) {
	if _, exists := collector.seen[ordinal]; exists {
		return
	}
	if uint64(ordinal) >= uint64(len(collector.snapshot.Records)) {
		collector.budget.exhausted = true
		collector.partial = true
		return
	}
	collector.seen[ordinal] = struct{}{}
	record := collector.snapshot.Records[ordinal]
	if !collector.predicate.permits(record) {
		return
	}
	if !collector.ranking.offer(record, tier) {
		collector.partial = true
	}
}

// matchWord unions the postings of every dictionary term matching one query
// word. Exact and prefix terms come from the sorted dictionary by binary
// search. Substring and fuzzy scans are progressive relaxation: they run
// only when the word matched nothing so far, so ordinary queries never scan
// the dictionary.
func (collector *candidateCollector) matchWord(word string, maximumDistance, maximumTerms int) []taggedOrdinal {
	best := make(map[uint32]int)
	termsMatched := 0
	stopped := false
	admitTerm := func(ordinals []uint32, tier int) bool {
		if termsMatched >= maximumTerms {
			collector.partial = true
			stopped = true
			return false
		}
		termsMatched++
		for _, ordinal := range ordinals {
			if !collector.budget.visitRecord() {
				collector.partial = true
				stopped = true
				return false
			}
			if current, exists := best[ordinal]; !exists || tier < current {
				best[ordinal] = tier
			}
		}
		return true
	}
	if exact := collector.snapshot.Query.TokenOrdinals(word); len(exact) != 0 {
		admitTerm(exact, tierExactToken)
	}
	terms := collector.snapshot.Query.TokenTerms()
	position := sort.SearchStrings(terms, word)
	for index := position; !stopped && index < len(terms) && strings.HasPrefix(terms[index], word); index++ {
		if terms[index] == word {
			continue
		}
		if !collector.budget.visitTerm() {
			collector.partial = true
			stopped = true
			break
		}
		admitTerm(collector.snapshot.Query.TokenOrdinals(terms[index]), tierPrefix)
	}
	if !stopped && len(best) == 0 {
		collector.scanDictionary(position, func(term string) (int, bool) {
			if strings.HasPrefix(term, word) || !strings.Contains(term, word) {
				return 0, false
			}
			return tierSubstring, true
		}, admitTerm)
	}
	if !stopped && len(best) == 0 && maximumDistance > 0 && utf8.RuneCountInString(word) >= minimumFuzzyRunes {
		wordRunes := utf8.RuneCountInString(word)
		collector.scanDictionary(position, func(term string) (int, bool) {
			if abs(utf8.RuneCountInString(term)-wordRunes) > maximumDistance {
				return 0, false
			}
			distance := editDistanceAtMost(word, term, maximumDistance)
			if distance == 0 || distance > maximumDistance {
				return 0, false
			}
			return tierFuzzyBase + distance, true
		}, admitTerm)
	}
	output := make([]taggedOrdinal, 0, len(best))
	for ordinal, tier := range best {
		output = append(output, taggedOrdinal{ordinal: ordinal, tier: tier})
	}
	sort.Slice(output, func(i, j int) bool { return output[i].ordinal < output[j].ordinal })
	return output
}

// scanDictionary examines dictionary terms once each against the term budget.
// When the dictionary exceeds the remaining budget the scan is windowed around
// the word's sorted position and the response is marked partial.
func (collector *candidateCollector) scanDictionary(position int, classify func(string) (int, bool), admit func([]uint32, int) bool) {
	terms := collector.snapshot.Query.TokenTerms()
	remaining := collector.budget.maximumTerms - collector.budget.terms
	if remaining <= 0 {
		if len(terms) != 0 {
			collector.partial = true
		}
		return
	}
	start, end := 0, len(terms)
	if len(terms) > remaining {
		start = max(0, position-remaining/2)
		end = min(len(terms), start+remaining)
		start = max(0, end-remaining)
		collector.partial = true
	}
	for _, term := range terms[start:end] {
		if !collector.budget.visitTerm() {
			collector.partial = true
			return
		}
		tier, matched := classify(term)
		if !matched {
			continue
		}
		if !admit(collector.snapshot.Query.TokenOrdinals(term), tier) {
			return
		}
	}
}

// intersectWords keeps ordinals present in every word's candidate set. The
// tier of a survivor is its worst tier across words, so a record matching one
// word exactly and another only fuzzily ranks as a fuzzy match.
func intersectWords(matches [][]taggedOrdinal) []taggedOrdinal {
	if len(matches) == 0 {
		return nil
	}
	smallest := 0
	for index := range matches {
		if len(matches[index]) < len(matches[smallest]) {
			smallest = index
		}
	}
	output := make([]taggedOrdinal, 0, len(matches[smallest]))
	for _, candidate := range matches[smallest] {
		tier := candidate.tier
		present := true
		for index, other := range matches {
			if index == smallest {
				continue
			}
			found := sort.Search(len(other), func(i int) bool { return other[i].ordinal >= candidate.ordinal })
			if found == len(other) || other[found].ordinal != candidate.ordinal {
				present = false
				break
			}
			tier = max(tier, other[found].tier)
		}
		if present {
			output = append(output, taggedOrdinal{ordinal: candidate.ordinal, tier: tier})
		}
	}
	return output
}

type filterPredicate struct {
	operation     wire.Operation
	filters       wire.Filters
	allowInferred bool
}

func newFilterPredicate(request wire.Request) filterPredicate {
	return filterPredicate{operation: request.Operation, filters: request.Filters, allowInferred: request.AllowInferred}
}

func (predicate filterPredicate) permits(record model.Record) bool {
	if !matchesOperation(record, predicate.operation) {
		return false
	}
	if !predicate.allowInferred && record.EvidenceClass != model.Verified {
		return false
	}
	return matchesFilters(record, predicate.filters)
}

// RepositoryMap consumes the payload-bound stable structural frontier when
// no explicit caller filter is present. With filters it drives a bounded group
// construction from the smallest persisted filter/path category.
func RepositoryMap(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits, len(snapshot.Records))
	// An empty query or an empty snapshot examines nothing and is not exhausted.
	if budget.maximum < 1 {
		return budget.response([]model.Record{}, 0, false)
	}
	if !hasExplicitFilters(request.Filters) {
		return repositoryMapFrontier(snapshot, request, budget)
	}
	plan := buildFilterPlan(snapshot, request, budget)
	if plan.impossible {
		return budget.response([]model.Record{}, 0, false)
	}
	byPath := make(map[string]rankedCandidate)
	pathOrder := make([]string, 0, len(plan.ordinals))
	for _, ordinal := range plan.ordinals {
		if !budget.visitRecord() {
			break
		}
		if uint64(ordinal) >= uint64(len(snapshot.Records)) {
			budget.exhausted = true
			continue
		}
		record := snapshot.Records[ordinal]
		if !structuralRecord(record) {
			continue
		}
		candidate := newMapCandidate(record)
		current, exists := byPath[record.Path]
		if !exists {
			byPath[record.Path] = candidate
			pathOrder = append(pathOrder, record.Path)
			continue
		}
		if compareRepresentativeCandidate(candidate, current) < 0 {
			byPath[record.Path] = candidate
		}
	}
	if budget.exhausted {
		return budget.response([]model.Record{}, 0, true)
	}
	ranking := newBoundedRanking(request.MaximumResults, budget)
	for _, path := range pathOrder {
		ranking.offerCandidate(byPath[path])
	}
	selected, omitted := ranking.records()
	// plan.partial only becomes true alongside budget.exhausted (buildFilterPlan
	// sets both together), and the exhausted path above already returned, so
	// plan.partial is always false here.
	return budget.response(selected, omitted, false)
}

func repositoryMapFrontier(snapshot store.Snapshot, request wire.Request, budget *workBudget) Response {
	groups, frontierPartial := snapshot.Query.MapGroups()
	ranking := newBoundedRanking(request.MaximumResults, budget)
	for _, group := range groups {
		if len(group.Ordinals) == 0 {
			budget.exhausted = true
			continue
		}
		if !budget.visitRecord() {
			break
		}
		ordinal := group.Ordinals[0]
		if uint64(ordinal) >= uint64(len(snapshot.Records)) {
			budget.exhausted = true
			continue
		}
		record := snapshot.Records[ordinal]
		if record.Path != group.Path {
			budget.exhausted = true
			continue
		}
		if !request.AllowInferred && record.EvidenceClass != model.Verified {
			continue
		}
		if !budget.visitRecord() {
			break
		}
		if !ranking.offerCandidate(newMapCandidate(record)) {
			break
		}
	}
	selected, omitted := ranking.records()
	return budget.response(selected, omitted, frontierPartial)
}

type filterPlan struct {
	ordinals   []uint32
	impossible bool
	partial    bool
}

type filterCategory struct {
	facet   store.QueryFacet
	values  map[string]struct{}
	ordered []string
	sources [][]uint32
	total   int
	path    bool
}

// buildFilterPlan serves RepositoryMap only. It intersects the persisted facet
// and path categories, charging one unit per driver posting entry visited.
func buildFilterPlan(snapshot store.Snapshot, request wire.Request, budget *workBudget) filterPlan {
	categories := make([]filterCategory, 0, 6)
	impossible := false
	addFacet := func(facet store.QueryFacet, values []string) {
		if len(values) == 0 || impossible {
			return
		}
		values = canonicalFilterValues(values)
		category := filterCategory{facet: facet, values: make(map[string]struct{}, len(values)), ordered: values, sources: make([][]uint32, 0, len(values))}
		for _, value := range values {
			category.values[value] = struct{}{}
			source := snapshot.Query.FacetOrdinals(facet, value)
			category.sources = append(category.sources, source)
			category.total += len(source)
		}
		if category.total == 0 {
			impossible = true
			return
		}
		categories = append(categories, category)
	}
	addPath := func(prefixes []string) {
		if len(prefixes) == 0 || impossible {
			return
		}
		prefixes = canonicalFilterValues(prefixes)
		paths := snapshot.Query.PathOrdinals()
		category := filterCategory{values: make(map[string]struct{}, len(prefixes)), ordered: prefixes, sources: make([][]uint32, 0, len(prefixes)), path: true}
		for _, prefix := range prefixes {
			start := lowerBoundPath(snapshot.Records, paths, prefix)
			end := lowerBoundPath(snapshot.Records, paths, prefix+"\U0010ffff")
			category.values[prefix] = struct{}{}
			source := paths[start:end]
			category.sources = append(category.sources, source)
			category.total += len(source)
		}
		if category.total == 0 {
			impossible = true
			return
		}
		categories = append(categories, category)
	}
	addFacet(store.QueryFacetLanguage, request.Filters.Languages)
	addFacet(store.QueryFacetKind, request.Filters.SymbolKinds)
	addFacet(store.QueryFacetSource, request.Filters.SourceTypes)
	addPath(request.Filters.PathPrefixes)
	if !request.AllowInferred {
		addFacet(store.QueryFacetEvidence, []string{string(model.Verified)})
	}
	if impossible || len(categories) == 0 {
		return filterPlan{impossible: impossible}
	}
	driver := 0
	for index := 1; index < len(categories); index++ {
		if categories[index].total < categories[driver].total {
			driver = index
		}
	}
	plan := filterPlan{ordinals: make([]uint32, 0, categories[driver].total)}
	allowed := make(map[uint32]struct{}, categories[driver].total)
	for _, source := range categories[driver].sources {
		for _, ordinal := range source {
			if !budget.visitRecord() {
				plan.partial = true
				return plan
			}
			if _, exists := allowed[ordinal]; exists {
				continue
			}
			if uint64(ordinal) >= uint64(len(snapshot.Records)) {
				budget.exhausted = true
				plan.partial = true
				return plan
			}
			record := snapshot.Records[ordinal]
			matched := true
			for index := range categories {
				if !categories[index].matchesRecord(record) {
					matched = false
					break
				}
				if index == driver || categories[index].path {
					continue
				}
				if !containsAnyOrdinal(categories[index].sources, ordinal) {
					matched = false
					break
				}
			}
			if !matched {
				continue
			}
			allowed[ordinal] = struct{}{}
			plan.ordinals = append(plan.ordinals, ordinal)
		}
	}
	if len(plan.ordinals) == 0 && !plan.partial {
		plan.impossible = true
	}
	return plan
}

func (category filterCategory) matchesRecord(record model.Record) bool {
	if category.path {
		for _, prefix := range category.ordered {
			if strings.HasPrefix(record.Path, prefix) {
				return true
			}
		}
		return false
	}
	var value string
	switch category.facet {
	case store.QueryFacetLanguage:
		value = record.Language
	case store.QueryFacetKind:
		value = string(record.RecordKind)
	case store.QueryFacetSource:
		value = record.SourceType
	case store.QueryFacetEvidence:
		value = string(record.EvidenceClass)
	default:
		return false
	}
	_, matched := category.values[value]
	return matched
}

func containsAnyOrdinal(sources [][]uint32, target uint32) bool {
	for _, source := range sources {
		position := sort.Search(len(source), func(i int) bool { return source[i] >= target })
		if position < len(source) && source[position] == target {
			return true
		}
	}
	return false
}

func canonicalFilterValues(values []string) []string {
	canonical := make([]string, 0, len(values))
	for _, value := range values {
		position := sort.SearchStrings(canonical, value)
		if position < len(canonical) && canonical[position] == value {
			continue
		}
		canonical = append(canonical, "")
		copy(canonical[position+1:], canonical[position:])
		canonical[position] = value
	}
	return canonical
}

func lowerBoundPath(records []model.Record, ordinals []uint32, value string) int {
	value = normalize(value)
	return sort.Search(len(ordinals), func(i int) bool {
		ordinal := ordinals[i]
		if uint64(ordinal) >= uint64(len(records)) {
			return true
		}
		return normalize(records[ordinal].Path) >= value
	})
}

func hasExplicitFilters(filters wire.Filters) bool {
	return len(filters.PathPrefixes) != 0 || len(filters.Languages) != 0 || len(filters.SymbolKinds) != 0 || len(filters.SourceTypes) != 0
}

func compareRepresentative(left, right model.Record) int {
	for _, comparison := range []int{
		cmp.Compare(evidenceTier(left), evidenceTier(right)),
		cmp.Compare(sourceTier(left.SourceType), sourceTier(right.SourceType)),
		cmp.Compare(mapKindTier(left.RecordKind), mapKindTier(right.RecordKind)),
		cmp.Compare(left.StartLine, right.StartLine),
		cmp.Compare(normalize(left.QualifiedName), normalize(right.QualifiedName)),
		cmp.Compare(left.Identity, right.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func sourceTier(source string) int {
	switch source {
	case "source":
		return 0
	case "document":
		return 1
	default:
		return 2
	}
}

// mapKindTier delegates to store.MapKindTier so the query planner's filtered
// maps and the store's persisted maps pick the same file representative.
func mapKindTier(kind model.RecordKind) int {
	return store.MapKindTier(kind)
}
