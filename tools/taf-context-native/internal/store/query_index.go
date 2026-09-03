package store

import (
	"cmp"
	"context"
	"slices"
	"sort"
	"strings"
	"unicode"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const (
	queryNamespace       = "~taf-query/"
	queryQualifiedPrefix = queryNamespace + "q/"
	queryShortPrefix     = queryNamespace + "s/"
	queryTokenPrefix     = queryNamespace + "t/"
	queryFacetPrefix     = queryNamespace + "f/"

	maximumQueryKeyBytes        = len(queryNamespace) + 3 + 512
	maximumQueryPostingTerms    = 1_000_000
	maximumQueryPostingOrdinals = 8_000_000
)

// QueryFacet names a canonical persisted filter dimension.
type QueryFacet string

const (
	QueryFacetLanguage  QueryFacet = "language"
	QueryFacetKind      QueryFacet = "kind"
	QueryFacetSource    QueryFacet = "source"
	QueryFacetEvidence  QueryFacet = "evidence"
	QueryFacetOperation QueryFacet = "operation"
)

const (
	queryOperationSymbols = "symbols"
	queryOperationDocs    = "docs"
)

// QueryMapGroup is one stable repository structure group. Ordinals are
// preference-ordered and refer to Snapshot.Records.
type QueryMapGroup struct {
	Path     string
	Ordinals []uint32
}

// QueryIndex is the immutable, payload-bound query section. Callers must treat
// returned slices as read-only.
type QueryIndex struct {
	postings   map[string][]uint32
	tokenTerms []string
	byPath     []uint32
	mapGroups  []QueryMapGroup
	mapPartial bool
}

func (index QueryIndex) Empty() bool {
	return len(index.postings) == 0 && len(index.tokenTerms) == 0 && len(index.byPath) == 0 && len(index.mapGroups) == 0 && !index.mapPartial
}

func (index QueryIndex) QualifiedOrdinals(value string) []uint32 {
	return index.postings[queryQualifiedPrefix+NormalizeQueryText(value)]
}

func (index QueryIndex) ShortOrdinals(value string) []uint32 {
	return index.postings[queryShortPrefix+NormalizeQueryText(value)]
}

func (index QueryIndex) TokenOrdinals(value string) []uint32 {
	return index.postings[queryTokenPrefix+NormalizeQueryText(value)]
}

func (index QueryIndex) FacetOrdinals(facet QueryFacet, value string) []uint32 {
	return index.postings[facetQueryKey(facet, value)]
}

func (index QueryIndex) TokenTerms() []string { return index.tokenTerms }

func (index QueryIndex) PathOrdinals() []uint32 { return index.byPath }

func (index QueryIndex) MapGroups() ([]QueryMapGroup, bool) { return index.mapGroups, index.mapPartial }

// BuildQueryIndex deterministically derives the same in-memory representation
// encoded in format v2. It is exported only so bounded query code and its
// direct immutable-snapshot tests share the store's canonical normalization.
func BuildQueryIndex(records []model.Record) QueryIndex {
	index, _ := buildQueryIndexContext(context.Background(), records, nil)
	return index
}

func buildQueryIndexContext(ctx context.Context, records []model.Record, observed func()) (QueryIndex, error) {
	return buildQueryIndexHintedContext(ctx, records, 0, observed)
}

// buildQueryIndexHintedContext accepts the persisted query posting count as a
// map sizing hint. The hint only reserves buckets: every key, ordinal, and
// count is still derived from the decoded records and compared against the
// persisted section by the caller.
func buildQueryIndexHintedContext(ctx context.Context, records []model.Record, postingHint int, observed func()) (QueryIndex, error) {
	index := QueryIndex{postings: make(map[string][]uint32, postingHint)}
	var visitor queryKeyVisitor
	for ordinal, record := range records {
		if ordinal%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if err := ctx.Err(); err != nil {
				return QueryIndex{}, err
			}
		}
		visitor.visit(record, func(key string) bool {
			index.postings[key] = append(index.postings[key], uint32(ordinal))
			return true
		})
	}
	index.tokenTerms = make([]string, 0, len(index.postings))
	for key := range index.postings {
		if strings.HasPrefix(key, queryTokenPrefix) {
			index.tokenTerms = append(index.tokenTerms, strings.TrimPrefix(key, queryTokenPrefix))
		}
	}
	if err := sortStringsContext(ctx, index.tokenTerms, observed); err != nil {
		return QueryIndex{}, err
	}
	index.byPath = make([]uint32, len(records))
	for ordinal := range records {
		index.byPath[ordinal] = uint32(ordinal)
	}
	canonical := newDirectRecords(records)
	comparisons := 0
	canceled := false
	slices.SortFunc(index.byPath, func(left, right uint32) int {
		comparisons++
		if comparisons%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if ctx.Err() != nil {
				canceled = true
				return 0
			}
		}
		return compareCanonicalPathOrdinal(canonical, left, right)
	})
	if canceled || ctx.Err() != nil {
		return QueryIndex{}, ctx.Err()
	}
	var err error
	index.mapGroups, index.mapPartial, err = buildCanonicalMapGroupsContext(ctx, canonical, index.byPath, policy.ProductionLimits().MaximumLexicalCandidates, observed)
	if err != nil {
		return QueryIndex{}, err
	}
	return index, nil
}

func sortStringsContext(ctx context.Context, values []string, observed func()) error {
	comparisons := 0
	canceled := false
	slices.SortFunc(values, func(left, right string) int {
		comparisons++
		if comparisons%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if ctx.Err() != nil {
				canceled = true
				return 0
			}
		}
		return cmp.Compare(left, right)
	})
	if canceled || ctx.Err() != nil {
		return ctx.Err()
	}
	return nil
}

// NormalizeQueryText is locale-independent and uses Unicode code points.
func NormalizeQueryText(value string) string { return strings.ToLower(strings.TrimSpace(value)) }

// QueryTokens splits separator and lower-to-upper transitions without losing
// Unicode validity. The complete normalized qualified name is indexed
// separately, so acronym runs intentionally remain one token.
func QueryTokens(value string) []string {
	return appendQueryTokens(make([]string, 0, 8), value)
}

// appendQueryTokens is QueryTokens with a caller-owned destination so a hot
// loop can reuse one buffer. The appended tokens are identical.
func appendQueryTokens(output []string, value string) []string {
	value = strings.TrimSpace(value)
	start := -1
	previousLower := false
	flush := func(end int) {
		if start >= 0 && start < end {
			output = append(output, NormalizeQueryText(value[start:end]))
		}
		start = -1
		previousLower = false
	}
	for offset, runeValue := range value {
		if !(unicode.IsLetter(runeValue) || unicode.IsDigit(runeValue)) {
			flush(offset)
			continue
		}
		if start < 0 {
			start = offset
		}
		if unicode.IsUpper(runeValue) && previousLower {
			flush(offset)
			start = offset
		}
		previousLower = unicode.IsLower(runeValue)
	}
	flush(len(value))
	return output
}

// QueryShortName is the normalized last dot-separated segment of a qualified
// name: the name an agent types for a method, function, or heading. The
// camel/snake parts of that segment are indexed separately as tokens.
func QueryShortName(value string) string {
	value = NormalizeQueryText(value)
	if last := strings.LastIndexByte(value, '.'); last >= 0 {
		value = value[last+1:]
	}
	return strings.TrimSpace(value)
}

func canonicalQueryKeys(record model.Record) []string {
	values := make([]string, 0, len(record.SearchTerms)+12)
	visitCanonicalQueryKeys(record, func(key string) bool {
		values = append(values, key)
		return true
	})
	sort.Strings(values)
	return values
}

func visitCanonicalQueryKeys(record model.Record, visit func(string) bool) bool {
	// One-shot callers size the buffer for the common case so a single visit
	// costs one allocation, as it did before the buffer became reusable.
	visitor := queryKeyVisitor{tokens: make([]string, 0, len(record.SearchTerms)+9)}
	return visitor.visit(record, visit)
}

// queryKeyVisitor emits exactly the keys visitCanonicalQueryKeys emits, in the
// same order, while reusing one token buffer across records so materializing a
// large index does not allocate per record.
type queryKeyVisitor struct {
	tokens []string
}

func (visitor *queryKeyVisitor) visit(record model.Record, visit func(string) bool) bool {
	if visit == nil {
		return false
	}
	qualified := NormalizeQueryText(record.QualifiedName)
	if !visit(queryQualifiedPrefix + qualified) {
		return false
	}
	if short := QueryShortName(record.QualifiedName); short != "" && !visit(queryShortPrefix+short) {
		return false
	}
	tokens := append(visitor.tokens[:0], qualified)
	tokens = appendQueryTokens(tokens, record.QualifiedName)
	tokens = append(tokens, record.SearchTerms...)
	visitor.tokens = tokens
	seen := tokens[:0]
	for _, token := range tokens {
		token = NormalizeQueryText(token)
		if token == "" || slices.Contains(seen, token) {
			continue
		}
		seen = append(seen, token)
		if !visit(queryTokenPrefix + token) {
			return false
		}
	}
	for _, key := range []string{
		facetQueryKey(QueryFacetLanguage, record.Language),
		facetQueryKey(QueryFacetKind, string(record.RecordKind)),
		facetQueryKey(QueryFacetSource, record.SourceType),
		facetQueryKey(QueryFacetEvidence, string(record.EvidenceClass)),
	} {
		if !visit(key) {
			return false
		}
	}
	if querySymbolRecord(record) {
		if !visit(facetQueryKey(QueryFacetOperation, queryOperationSymbols)) {
			return false
		}
	}
	if queryDocumentRecord(record) {
		if !visit(facetQueryKey(QueryFacetOperation, queryOperationDocs)) {
			return false
		}
	}
	return true
}

func facetQueryKey(facet QueryFacet, value string) string {
	return queryFacetPrefix + string(facet) + "/" + value
}

func querySymbolRecord(record model.Record) bool {
	return record.RecordKind != model.Heading && record.RecordKind != model.DocumentChunk && record.SourceType != "document"
}

func queryDocumentRecord(record model.Record) bool {
	return record.RecordKind == model.Heading || record.RecordKind == model.DocumentChunk || record.SourceType == "document"
}

// canonicalRecords exposes the records the canonical path order sorts, in the
// ordinal space of that order, together with the normalized path and
// qualified-name keys the comparator needs. The keys are computed once per
// record instead of once per comparison; the comparison values, and therefore
// the total order, are unchanged.
type canonicalRecords interface {
	At(uint32) model.Record
	NormalizedPath(uint32) string
	NormalizedName(uint32) string
}

type directRecords struct {
	records         []model.Record
	normalizedPaths []string
	normalizedNames []string
}

func newDirectRecords(records []model.Record) directRecords {
	paths, names := normalizedCanonicalKeys(len(records), func(ordinal int) model.Record { return records[ordinal] })
	return directRecords{records: records, normalizedPaths: paths, normalizedNames: names}
}

func (records directRecords) At(ordinal uint32) model.Record { return records.records[ordinal] }

func (records directRecords) NormalizedPath(ordinal uint32) string {
	return records.normalizedPaths[ordinal]
}

func (records directRecords) NormalizedName(ordinal uint32) string {
	return records.normalizedNames[ordinal]
}

type recordsByCanonicalOrder struct {
	records         []model.Record
	order           []uint32
	normalizedPaths []string
	normalizedNames []string
}

func newRecordsByCanonicalOrder(records []model.Record, order []uint32) recordsByCanonicalOrder {
	paths, names := normalizedCanonicalKeys(len(order), func(ordinal int) model.Record { return records[order[ordinal]] })
	return recordsByCanonicalOrder{records: records, order: order, normalizedPaths: paths, normalizedNames: names}
}

func (records recordsByCanonicalOrder) At(ordinal uint32) model.Record {
	return records.records[records.order[ordinal]]
}

func (records recordsByCanonicalOrder) NormalizedPath(ordinal uint32) string {
	return records.normalizedPaths[ordinal]
}

func (records recordsByCanonicalOrder) NormalizedName(ordinal uint32) string {
	return records.normalizedNames[ordinal]
}

func normalizedCanonicalKeys(count int, at func(int) model.Record) ([]string, []string) {
	paths := make([]string, count)
	names := make([]string, count)
	for ordinal := range count {
		record := at(ordinal)
		paths[ordinal] = NormalizeQueryText(record.Path)
		names[ordinal] = NormalizeQueryText(record.QualifiedName)
	}
	return paths, names
}

func compareCanonicalPathOrdinal(records canonicalRecords, left, right uint32) int {
	if comparison := cmp.Compare(records.NormalizedPath(left), records.NormalizedPath(right)); comparison != 0 {
		return comparison
	}
	l, r := records.At(left), records.At(right)
	if comparison := cmp.Compare(l.Path, r.Path); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(l.StartLine, r.StartLine); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(string(l.RecordKind), string(r.RecordKind)); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(records.NormalizedName(left), records.NormalizedName(right)); comparison != 0 {
		return comparison
	}
	return cmp.Compare(l.Identity, r.Identity)
}

func buildMapGroups(records []model.Record, pathOrdinals []uint32, maximum int) ([]QueryMapGroup, bool) {
	groups, partial, _ := buildCanonicalMapGroupsContext(context.Background(), newDirectRecords(records), pathOrdinals, maximum, nil)
	return groups, partial
}

func buildIndexedMapGroups(records []model.Record, recordOrder, pathOrdinals []uint32, maximum int) ([]QueryMapGroup, bool) {
	groups, partial, _ := buildCanonicalMapGroupsContext(context.Background(), newRecordsByCanonicalOrder(records, recordOrder), pathOrdinals, maximum, nil)
	return groups, partial
}

func buildCanonicalMapGroupsContext(ctx context.Context, records canonicalRecords, pathOrdinals []uint32, maximum int, observed func()) ([]QueryMapGroup, bool, error) {
	if maximum < 1 {
		return nil, len(pathOrdinals) != 0, nil
	}
	groups := make([]QueryMapGroup, 0, min(len(pathOrdinals), maximum))
	partial := false
	visited := 0
	for start := 0; start < len(pathOrdinals); {
		visited++
		if visited%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if err := ctx.Err(); err != nil {
				return nil, false, err
			}
		}
		end := start + 1
		path := records.At(pathOrdinals[start]).Path
		best := pathOrdinals[start]
		for end < len(pathOrdinals) && records.At(pathOrdinals[end]).Path == path {
			visited++
			if visited%contextCheckInterval == 0 {
				if observed != nil {
					observed()
				}
				if err := ctx.Err(); err != nil {
					return nil, false, err
				}
			}
			if compareCanonicalMapRepresentative(records, pathOrdinals[end], best) < 0 {
				best = pathOrdinals[end]
			}
			end++
		}
		if len(groups) == maximum {
			partial = true
			break
		}
		groups = append(groups, QueryMapGroup{Path: path, Ordinals: []uint32{best}})
		start = end
	}
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	return groups, partial, nil
}

func compareCanonicalMapRepresentative(records canonicalRecords, leftOrdinal, rightOrdinal uint32) int {
	left, right := records.At(leftOrdinal), records.At(rightOrdinal)
	if comparison := cmp.Compare(queryEvidenceTier(left), queryEvidenceTier(right)); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(querySourceTier(left), querySourceTier(right)); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(MapKindTier(left.RecordKind), MapKindTier(right.RecordKind)); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(left.StartLine, right.StartLine); comparison != 0 {
		return comparison
	}
	if comparison := cmp.Compare(records.NormalizedName(leftOrdinal), records.NormalizedName(rightOrdinal)); comparison != 0 {
		return comparison
	}
	return cmp.Compare(left.Identity, right.Identity)
}

func queryEvidenceTier(record model.Record) int {
	switch record.EvidenceClass {
	case model.Verified:
		return 0
	case model.Inferred:
		return 1
	default:
		return 2
	}
}

func querySourceTier(record model.Record) int {
	switch record.SourceType {
	case "source":
		return 0
	case "document":
		return 1
	default:
		return 2
	}
}

// MapKindTier orders the records that can represent one file in a
// repository map: the module itself, then what the file defines, then its
// headings, configuration keys, document chunks, and finally its imports.
// The store and the query planner share it so persisted and filtered maps
// pick the same representative.
func MapKindTier(kind model.RecordKind) int {
	switch kind {
	case model.Module:
		return 0
	case model.Definition, model.EntryPoint:
		return 1
	case model.Heading:
		return 2
	case model.Configuration:
		return 3
	case model.DocumentChunk:
		return 4
	case model.Import:
		return 5
	default:
		return 6
	}
}

func validQueryKey(key string) bool {
	if len(key) == 0 || len(key) > maximumQueryKeyBytes || !strings.HasPrefix(key, queryNamespace) {
		return false
	}
	for _, prefix := range []string{queryQualifiedPrefix, queryShortPrefix, queryTokenPrefix} {
		if strings.HasPrefix(key, prefix) {
			value := strings.TrimPrefix(key, prefix)
			return value != "" && NormalizeQueryText(value) == value
		}
	}
	for _, facet := range []QueryFacet{QueryFacetLanguage, QueryFacetKind, QueryFacetSource, QueryFacetEvidence, QueryFacetOperation} {
		prefix := queryFacetPrefix + string(facet) + "/"
		if strings.HasPrefix(key, prefix) {
			return len(key) > len(prefix)
		}
	}
	return false
}

func recordHasQueryKey(record model.Record, key string) bool {
	keys := canonicalQueryKeys(record)
	_, found := slices.BinarySearch(keys, key)
	return found
}
