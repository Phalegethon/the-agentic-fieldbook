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
	index := QueryIndex{postings: make(map[string][]uint32)}
	for ordinal, record := range records {
		if ordinal%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if err := ctx.Err(); err != nil {
				return QueryIndex{}, err
			}
		}
		visitCanonicalQueryKeys(record, func(key string) bool {
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
		return comparePathOrdinal(records, left, right)
	})
	if canceled || ctx.Err() != nil {
		return QueryIndex{}, ctx.Err()
	}
	var err error
	index.mapGroups, index.mapPartial, err = buildCanonicalMapGroupsContext(ctx, directRecords(records), index.byPath, policy.ProductionLimits().MaximumLexicalCandidates, observed)
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
	value = strings.TrimSpace(value)
	output := make([]string, 0, 8)
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

func QueryShortName(value string) string {
	parts := QueryTokens(value)
	if len(parts) == 0 {
		return ""
	}
	return parts[len(parts)-1]
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
	nameTokens := QueryTokens(record.QualifiedName)
	tokens := make([]string, 0, len(record.SearchTerms)+len(nameTokens)+1)
	tokens = append(tokens, qualified)
	tokens = append(tokens, nameTokens...)
	tokens = append(tokens, record.SearchTerms...)
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

func comparePathOrdinal(records []model.Record, left, right uint32) int {
	return compareCanonicalPathOrdinal(directRecords(records), left, right)
}

func compareIndexedPathOrdinal(records []model.Record, recordOrder []uint32, left, right uint32) int {
	return compareCanonicalPathOrdinal(recordsByCanonicalOrder{records: records, order: recordOrder}, left, right)
}

type canonicalRecords interface {
	At(uint32) model.Record
}

type directRecords []model.Record

func (records directRecords) At(ordinal uint32) model.Record { return records[ordinal] }

type recordsByCanonicalOrder struct {
	records []model.Record
	order   []uint32
}

func (records recordsByCanonicalOrder) At(ordinal uint32) model.Record {
	return records.records[records.order[ordinal]]
}

func compareCanonicalPathOrdinal(records canonicalRecords, left, right uint32) int {
	l, r := records.At(left), records.At(right)
	for _, comparison := range []int{
		cmp.Compare(NormalizeQueryText(l.Path), NormalizeQueryText(r.Path)),
		cmp.Compare(l.Path, r.Path),
		cmp.Compare(l.StartLine, r.StartLine),
		cmp.Compare(string(l.RecordKind), string(r.RecordKind)),
		cmp.Compare(NormalizeQueryText(l.QualifiedName), NormalizeQueryText(r.QualifiedName)),
		cmp.Compare(l.Identity, r.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func buildMapGroups(records []model.Record, pathOrdinals []uint32, maximum int) ([]QueryMapGroup, bool) {
	groups, partial, _ := buildCanonicalMapGroupsContext(context.Background(), directRecords(records), pathOrdinals, maximum, nil)
	return groups, partial
}

func buildIndexedMapGroups(records []model.Record, recordOrder, pathOrdinals []uint32, maximum int) ([]QueryMapGroup, bool) {
	groups, partial, _ := buildCanonicalMapGroupsContext(context.Background(), recordsByCanonicalOrder{records: records, order: recordOrder}, pathOrdinals, maximum, nil)
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
			if compareMapRepresentative(records.At(pathOrdinals[end]), records.At(best)) < 0 {
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

func compareMapRepresentative(left, right model.Record) int {
	for _, comparison := range []int{
		cmp.Compare(queryEvidenceTier(left), queryEvidenceTier(right)),
		cmp.Compare(querySourceTier(left), querySourceTier(right)),
		cmp.Compare(queryMapKindTier(left.RecordKind), queryMapKindTier(right.RecordKind)),
		cmp.Compare(left.StartLine, right.StartLine),
		cmp.Compare(NormalizeQueryText(left.QualifiedName), NormalizeQueryText(right.QualifiedName)),
		cmp.Compare(left.Identity, right.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
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

func queryMapKindTier(kind model.RecordKind) int {
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
