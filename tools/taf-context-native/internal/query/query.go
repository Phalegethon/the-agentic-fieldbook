// Package query selects bounded, deterministic evidence from an immutable store snapshot.
package query

import (
	"cmp"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

type Response struct {
	Records    []model.Record
	Omitted    int
	Partial    bool
	Counters   model.WorkCounters
	TermVisits int
}

type workBudget struct {
	records      int
	maximum      int
	terms        int
	maximumTerms int
	exhausted    bool
}

func newWorkBudget(limits policy.Limits) *workBudget {
	return &workBudget{maximum: max(0, limits.MaximumLexicalCandidates), maximumTerms: max(0, limits.MaximumFuzzyTerms)}
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

// Search plans one bounded union over persisted exact postings, the stable
// lexical-record frontier, and the bounded prefix/fuzzy token window.
func Search(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits)
	queryText := normalize(deref(request.Query))
	if queryText == "" || budget.maximum < 1 || budget.maximumTerms < 1 {
		return budget.response([]model.Record{}, 0, queryText != "" || len(snapshot.Records) != 0)
	}
	plan := buildFilterPlan(snapshot, request, budget, true)
	if plan.impossible {
		return budget.response([]model.Record{}, 0, false)
	}
	collector := candidateCollector{
		snapshot: snapshot,
		budget:   budget,
		plan:     plan,
		seen:     make(map[uint32]struct{}, min(len(snapshot.Records), budget.maximum)),
		ranking:  newBoundedRanking(request.MaximumResults, budget),
	}

	seenExactTerms := make(map[string]struct{}, 8)
	probe := func(key string, ordinals []uint32, tier int) {
		if key == "" {
			return
		}
		if _, exists := seenExactTerms[key]; exists {
			return
		}
		seenExactTerms[key] = struct{}{}
		if budget.visitTerm() {
			collector.intersect(ordinals, tier)
		}
	}
	queryTokens := tokens(queryText)
	probe("qualified/"+queryText, snapshot.Query.QualifiedOrdinals(queryText), 0)
	if short := shortName(queryText); short != "" && len(queryTokens) == 1 {
		probe("short/"+short, snapshot.Query.ShortOrdinals(short), 1)
	}
	probe("token/"+queryText, snapshot.Query.TokenOrdinals(queryText), 2)
	incompleteTerms := probeFallbackTerms(&collector, queryText, seenExactTerms)
	// Treat a multi-token phrase as one intent. Unioning broad component words
	// (for example "level" and "one") can exhaust the record budget and outrank
	// the exact or fuzzy phrase the caller actually supplied.
	if len(queryTokens) == 1 {
		probe("token/"+queryTokens[0], snapshot.Query.TokenOrdinals(queryTokens[0]), 2)
	}

	incompleteSubstring := probeSubstringFrontier(&collector, queryText)
	selected, omitted := collector.ranking.records()
	return budget.response(selected, omitted, plan.partial || collector.partial || incompleteSubstring || incompleteTerms || omitted > 0)
}

// probeSubstringFrontier admits the otherwise non-indexable substring tier
// from a persisted, path-stable ordinal frontier. Exact postings remain
// unbounded by this frontier; truncation is always surfaced as Partial.
func probeSubstringFrontier(collector *candidateCollector, queryText string) bool {
	ordinals := collector.snapshot.Query.PathOrdinals()
	frontierPartial := len(ordinals) > collector.budget.maximum
	if len(ordinals) > collector.budget.maximum {
		ordinals = ordinals[:collector.budget.maximum]
	}
	if collector.plan.active {
		ordinals = collector.plan.ordinals
		frontierPartial = collector.plan.partial
	}
	for _, ordinal := range ordinals {
		if !collector.budget.visitRecord() {
			return true
		}
		if uint64(ordinal) >= uint64(len(collector.snapshot.Records)) {
			collector.budget.exhausted = true
			return true
		}
		if _, exists := collector.seen[ordinal]; exists {
			continue
		}
		record := collector.snapshot.Records[ordinal]
		seenTerms := make(map[string]struct{}, min(len(record.SearchTerms)+4, collector.budget.maximumTerms))
		matched := false
		visitTerm := func(term string) bool {
			if !collector.budget.visitTerm() {
				return false
			}
			term = normalize(term)
			if term == "" {
				return true
			}
			if _, exists := seenTerms[term]; exists {
				return true
			}
			seenTerms[term] = struct{}{}
			if strings.Contains(term, queryText) {
				matched = true
				return false
			}
			return true
		}
		complete := visitTerm(record.QualifiedName)
		if complete && !matched {
			for _, term := range tokens(record.QualifiedName) {
				if !visitTerm(term) {
					complete = false
					break
				}
			}
		}
		if complete && !matched {
			for _, term := range record.SearchTerms {
				if !visitTerm(term) {
					complete = false
					break
				}
			}
		}
		if matched {
			collector.append(ordinal, 3)
		}
		if !complete && !matched {
			return true
		}
	}
	return frontierPartial
}

// probeFallbackTerms examines either the entire small persisted token
// dictionary or one deterministic window around its binary-search position.
// The window contains both prefix successors and fuzzy predecessors, and its
// incompleteness is reported instead of silently overclaiming completeness.
func probeFallbackTerms(collector *candidateCollector, queryText string, exact map[string]struct{}) bool {
	terms := collector.snapshot.Query.TokenTerms()
	if len(terms) == 0 || collector.budget.exhausted {
		return false
	}
	remaining := collector.budget.maximumTerms - collector.budget.terms
	if remaining <= 0 {
		return len(terms) != 0
	}
	start, end := 0, len(terms)
	incomplete := false
	if len(terms) > remaining {
		position := lowerBoundTerm(terms, queryText, collector.budget)
		remaining = collector.budget.maximumTerms - collector.budget.terms
		if remaining <= 0 {
			return true
		}
		start = max(0, position-remaining/3)
		end = min(len(terms), start+remaining)
		start = max(0, end-remaining)
		incomplete = start != 0 || end != len(terms)
	}
	for _, term := range terms[start:end] {
		if !collector.budget.visitTerm() {
			return true
		}
		if _, exists := exact["token/"+term]; exists {
			continue
		}
		if strings.HasPrefix(term, queryText) {
			collector.intersect(collector.snapshot.Query.TokenOrdinals(term), 3)
			continue
		}
		distance := editDistanceAtMost(queryText, term, 2)
		if distance > 0 && distance <= 2 {
			collector.intersect(collector.snapshot.Query.TokenOrdinals(term), 3+distance)
		}
	}
	return incomplete
}

func lowerBoundTerm(terms []string, queryText string, budget *workBudget) int {
	low, high := 0, len(terms)
	for low < high {
		if !budget.visitTerm() {
			return low
		}
		middle := low + (high-low)/2
		if terms[middle] < queryText {
			low = middle + 1
		} else {
			high = middle
		}
	}
	return low
}

// RepositoryMap consumes the payload-bound stable structural frontier when
// no explicit caller filter is present. With filters it drives a bounded group
// construction from the smallest persisted filter/path category, allowing a
// selective hit beyond the structural frontier without scanning raw records.
func RepositoryMap(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits)
	if budget.maximum < 1 {
		return budget.response([]model.Record{}, 0, len(snapshot.Records) != 0)
	}
	if !hasExplicitFilters(request.Filters) {
		return repositoryMapFrontier(snapshot, request, budget)
	}
	plan := buildFilterPlan(snapshot, request, budget, false)
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
		candidate := newRankedCandidate(record, 0)
		current, exists := byPath[record.Path]
		if !exists {
			byPath[record.Path] = candidate
			pathOrder = append(pathOrder, record.Path)
			continue
		}
		if !budget.visitRecord() {
			break
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
		candidate := byPath[path]
		if !ranking.offerCandidate(candidate) {
			break
		}
	}
	selected, omitted := ranking.records()
	return budget.response(selected, omitted, plan.partial || omitted > 0)
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
		if !ranking.offer(record, 0) {
			break
		}
	}
	selected, omitted := ranking.records()
	return budget.response(selected, omitted, frontierPartial || omitted > 0)
}

type candidateCollector struct {
	snapshot store.Snapshot
	budget   *workBudget
	plan     filterPlan
	seen     map[uint32]struct{}
	ranking  boundedRanking
	partial  bool
}

func (collector *candidateCollector) intersect(posting []uint32, tier int) {
	if collector.budget.exhausted || len(posting) == 0 || collector.plan.impossible {
		return
	}
	if collector.plan.active && len(collector.plan.ordinals) < len(posting) {
		for _, ordinal := range collector.plan.ordinals {
			if !containsOrdinal(posting, ordinal, collector.budget) {
				if collector.budget.exhausted {
					collector.partial = true
					return
				}
				continue
			}
			collector.append(ordinal, tier)
		}
		return
	}
	for _, ordinal := range posting {
		if !collector.budget.visitRecord() {
			collector.partial = true
			return
		}
		if collector.plan.active {
			if _, allowed := collector.plan.allowed[ordinal]; !allowed {
				continue
			}
		}
		collector.append(ordinal, tier)
	}
}

func (collector *candidateCollector) append(ordinal uint32, tier int) {
	if _, exists := collector.seen[ordinal]; exists {
		return
	}
	if uint64(ordinal) >= uint64(len(collector.snapshot.Records)) {
		collector.budget.exhausted = true
		return
	}
	record := collector.snapshot.Records[ordinal]
	if !collector.plan.active {
		if !collector.plan.permits(collector.snapshot, ordinal, collector.budget) {
			if collector.budget.exhausted {
				collector.partial = true
			}
			return
		}
	}
	collector.seen[ordinal] = struct{}{}
	if !collector.ranking.offer(record, tier) {
		collector.partial = true
	}
}

func (plan filterPlan) permits(snapshot store.Snapshot, ordinal uint32, budget *workBudget) bool {
	if uint64(ordinal) >= uint64(len(snapshot.Records)) {
		budget.exhausted = true
		return false
	}
	record := snapshot.Records[ordinal]
	for _, category := range plan.categories {
		if !category.matchesRecord(record) {
			return false
		}
		if category.path {
			continue
		}
		if !containsAnyOrdinal(category.sources, ordinal, budget) {
			return false
		}
	}
	return true
}

func containsOrdinal(ordinals []uint32, target uint32, budget *workBudget) bool {
	low, high := 0, len(ordinals)
	for low < high {
		if !budget.visitRecord() {
			return false
		}
		middle := low + (high-low)/2
		if ordinals[middle] < target {
			low = middle + 1
		} else {
			high = middle
		}
	}
	return low < len(ordinals) && ordinals[low] == target
}

type filterPlan struct {
	ordinals   []uint32
	allowed    map[uint32]struct{}
	categories []filterCategory
	active     bool
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

func buildFilterPlan(snapshot store.Snapshot, request wire.Request, budget *workBudget, includeOperation bool) filterPlan {
	explicit := hasExplicitFilters(request.Filters)
	categories := make([]filterCategory, 0, 7)
	impossible := false
	incomplete := false
	addFacet := func(facet store.QueryFacet, values []string) {
		if len(values) == 0 || impossible || incomplete {
			return
		}
		var complete bool
		values, complete = canonicalFilterValues(values, budget)
		if !complete {
			incomplete = true
			return
		}
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
		if len(prefixes) == 0 || impossible || incomplete {
			return
		}
		var complete bool
		prefixes, complete = canonicalFilterValues(prefixes, budget)
		if !complete {
			incomplete = true
			return
		}
		paths := snapshot.Query.PathOrdinals()
		category := filterCategory{values: make(map[string]struct{}, len(prefixes)), ordered: prefixes, sources: make([][]uint32, 0, len(prefixes)), path: true}
		for _, prefix := range prefixes {
			start := lowerBoundPath(snapshot.Records, paths, prefix, budget)
			end := lowerBoundPath(snapshot.Records, paths, prefix+"\U0010ffff", budget)
			if budget.exhausted {
				incomplete = true
				return
			}
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

	// Explicit request filters are added first. They form the selective fused
	// precheck before the operation/evidence sources are verified.
	addFacet(store.QueryFacetLanguage, request.Filters.Languages)
	addFacet(store.QueryFacetKind, request.Filters.SymbolKinds)
	addFacet(store.QueryFacetSource, request.Filters.SourceTypes)
	addPath(request.Filters.PathPrefixes)
	if includeOperation {
		switch request.Operation {
		case wire.SearchSymbols:
			addFacet(store.QueryFacetOperation, []string{"symbols"})
		case wire.SearchDocs:
			addFacet(store.QueryFacetOperation, []string{"docs"})
		}
	}
	if !request.AllowInferred {
		addFacet(store.QueryFacetEvidence, []string{string(model.Verified)})
	}
	if incomplete {
		return filterPlan{active: true, partial: true}
	}
	if impossible || len(categories) == 0 {
		return filterPlan{active: true, impossible: impossible}
	}
	if !explicit {
		return filterPlan{categories: categories}
	}

	// Pick a driver only after retaining every category. The final ordinals are
	// verified against every other persisted source before query admission.
	driver := 0
	for index := 1; index < len(categories); index++ {
		if !budget.visitRecord() {
			return filterPlan{active: true, partial: true}
		}
		if categories[index].total < categories[driver].total {
			driver = index
		}
	}
	capacity := min(categories[driver].total, max(0, budget.maximum-budget.records))
	plan := filterPlan{
		ordinals:   make([]uint32, 0, capacity),
		allowed:    make(map[uint32]struct{}, capacity),
		categories: categories,
		active:     true,
	}
	for _, source := range categories[driver].sources {
		for _, ordinal := range source {
			if !budget.visitRecord() {
				plan.partial = true
				return plan
			}
			if _, exists := plan.allowed[ordinal]; exists {
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
			}
			if !matched {
				continue
			}
			for index := range categories {
				if index == driver || categories[index].path {
					continue
				}
				if !containsAnyOrdinal(categories[index].sources, ordinal, budget) {
					if budget.exhausted {
						plan.partial = true
						return plan
					}
					matched = false
					break
				}
			}
			if !matched {
				continue
			}
			plan.allowed[ordinal] = struct{}{}
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
	case store.QueryFacetOperation:
		if _, symbols := category.values["symbols"]; symbols && matchesOperation(record, wire.SearchSymbols) {
			return true
		}
		if _, docs := category.values["docs"]; docs && matchesOperation(record, wire.SearchDocs) {
			return true
		}
		return false
	default:
		return false
	}
	_, matched := category.values[value]
	return matched
}

func containsAnyOrdinal(sources [][]uint32, target uint32, budget *workBudget) bool {
	for _, source := range sources {
		if containsOrdinal(source, target, budget) {
			return true
		}
		if budget.exhausted {
			return false
		}
	}
	return false
}

func canonicalFilterValues(values []string, budget *workBudget) ([]string, bool) {
	canonical := make([]string, 0, len(values))
	for inputIndex, value := range values {
		// Reserve the worst-case binary-search and equality comparisons for
		// this input position. The reservation is independent of caller order,
		// while still covering every comparison actually performed below.
		comparisonCredits := 1
		for size := inputIndex; size > 0; size >>= 1 {
			comparisonCredits++
		}
		for range comparisonCredits {
			if !budget.visitRecord() {
				return nil, false
			}
		}
		low, high := 0, len(canonical)
		for low < high {
			middle := low + (high-low)/2
			if canonical[middle] < value {
				low = middle + 1
			} else {
				high = middle
			}
		}
		if low < len(canonical) {
			if canonical[low] == value {
				continue
			}
		}
		canonical = append(canonical, "")
		copy(canonical[low+1:], canonical[low:])
		canonical[low] = value
	}
	return canonical, true
}

func lowerBoundPath(records []model.Record, ordinals []uint32, value string, budget *workBudget) int {
	value = normalize(value)
	low, high := 0, len(ordinals)
	for low < high {
		if !budget.visitRecord() {
			return low
		}
		middle := low + (high-low)/2
		ordinal := ordinals[middle]
		if uint64(ordinal) >= uint64(len(records)) {
			budget.exhausted = true
			return low
		}
		if normalize(records[ordinal].Path) < value {
			low = middle + 1
		} else {
			high = middle
		}
	}
	return low
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

func mapKindTier(kind model.RecordKind) int {
	switch kind {
	case model.Module:
		return 0
	case model.Heading:
		return 1
	case model.DocumentChunk:
		return 2
	default:
		return 3
	}
}
