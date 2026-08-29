package query

import (
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestSearchExactPostingFindsHitBeyondCandidateFrontier(t *testing.T) {
	records := make([]model.Record, 0, 4097)
	for index := 0; index < 4096; index++ {
		records = append(records, testRecord(index, "noise", model.Definition, model.Verified))
	}
	hit := testRecord(4096, "needle.Service", model.Definition, model.Verified)
	hit.SearchTerms = []string{"needle", "service"}
	records = append(records, hit)
	snapshot := indexedSnapshot(records)

	response := Search(snapshot, searchRequest("needle"), policy.ProductionLimits())
	if len(response.Records) != 1 || response.Records[0].Identity != hit.Identity {
		t.Fatalf("records = %#v, want exact posting hit %#v", response.Records, hit)
	}
	if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates {
		t.Fatalf("response = %#v", response)
	}
}

func TestSearchFiltersBeforeRankingAndNeverPromotesInferredByDefault(t *testing.T) {
	verified := testRecord(0, "pkg.Service", model.Definition, model.Verified)
	inferred := testRecord(1, "pkg.Service", model.Definition, model.Inferred)
	inferred.Path = "aaa/generated.go"
	filtered := testRecord(2, "pkg.Service", model.Definition, model.Verified)
	filtered.Language = "python"
	snapshot := indexedSnapshot([]model.Record{verified, inferred, filtered})
	request := searchRequest("service")
	request.Filters.Languages = []string{"go"}
	response := Search(snapshot, request, policy.ProductionLimits())
	if len(response.Records) != 1 || response.Records[0].Identity != verified.Identity {
		t.Fatalf("default filtered records = %#v", response.Records)
	}
	request.AllowInferred = true
	response = Search(snapshot, request, policy.ProductionLimits())
	if got, want := identities(response.Records), []string{verified.Identity, inferred.Identity}; !reflect.DeepEqual(got, want) {
		t.Fatalf("allowed inferred identities = %#v, want %#v", got, want)
	}
}

func TestSearchPrefixFallbackIsBoundedAndExplicitlyPartial(t *testing.T) {
	records := make([]model.Record, 0, 4097)
	for index := 0; index < 4097; index++ {
		record := testRecord(index, fmt.Sprintf("prefix%d", index), model.Definition, model.Verified)
		record.SearchTerms = []string{fmt.Sprintf("prefix%d", index)}
		records = append(records, record)
	}
	response := Search(indexedSnapshot(records), searchRequest("prefix"), policy.ProductionLimits())
	if !response.Partial || response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.TermVisits > policy.ProductionLimits().MaximumFuzzyTerms || response.Omitted != 0 {
		t.Fatalf("bounded prefix response = %#v", response)
	}
}

func TestSearchIsPermutationInvariantAndKeepsVerifiedAtEqualTier(t *testing.T) {
	verified := testRecord(0, "z.Service", model.Definition, model.Verified)
	inferred := testRecord(1, "a.Service", model.Definition, model.Inferred)
	left := indexedSnapshot([]model.Record{verified, inferred})
	right := indexedSnapshot([]model.Record{inferred, verified})
	first := Search(left, searchRequest("service"), policy.ProductionLimits())
	second := Search(right, searchRequest("service"), policy.ProductionLimits())
	if got, want := identities(first.Records), []string{verified.Identity}; !reflect.DeepEqual(got, want) {
		t.Fatalf("verified ordering = %#v, want %#v", got, want)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("permutation changed response:\nleft=%#v\nright=%#v", first, second)
	}
}

func TestRepositoryMapUsesBoundedDeterministicRepresentatives(t *testing.T) {
	records := []model.Record{
		testRecord(0, "z/module.go", model.Module, model.Verified),
		testRecord(1, "a/module.go", model.Module, model.Verified),
		testRecord(2, "a/module.go", model.Definition, model.Verified),
	}
	records[0].Path = "z/module.go"
	records[1].Path = "a/module.go"
	records[2].Path = "a/module.go"
	response := RepositoryMap(indexedSnapshot(records), mapRequest(), policy.ProductionLimits())
	if got, want := identities(response.Records), []string{records[1].Identity, records[0].Identity}; !reflect.DeepEqual(got, want) {
		t.Fatalf("representatives = %#v, want %#v", got, want)
	}
	if response.Counters.ConsideredRecords <= 2 || response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.Partial {
		t.Fatalf("map response = %#v", response)
	}
}

func TestTokensSplitCommonCamelSnakeKebabAndDottedNames(t *testing.T) {
	if got, want := tokens("HTTPServer.parse_value-name"), []string{"httpserver", "parse", "value", "name"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("tokens = %#v, want %#v", got, want)
	}
}

func TestSearchSupportsQualifiedShortAliasPrefixFuzzyAndDocumentSeparation(t *testing.T) {
	symbol := testRecord(0, "pkg.Service", model.Definition, model.Verified)
	symbol.SearchTerms = []string{"alias", "service"}
	document := testRecord(1, "Guide Setup", model.Heading, model.Verified)
	document.SourceType, document.Language, document.SearchTerms = "document", "markdown", []string{"guide", "setup"}
	snapshot := indexedSnapshot([]model.Record{symbol, document})
	for _, text := range []string{"pkg.Service", "service", "alias", "serv", "servce"} {
		response := Search(snapshot, searchRequest(text), policy.ProductionLimits())
		if got := identities(response.Records); !reflect.DeepEqual(got, []string{symbol.Identity}) {
			t.Fatalf("%q symbols = %#v", text, got)
		}
	}
	request := searchRequest("setup")
	request.Operation = wire.SearchDocs
	response := Search(snapshot, request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{document.Identity}) {
		t.Fatalf("docs = %#v", got)
	}
}

func TestSearchUsesPersistedFuzzyTermsBeforeSubstringFrontierExhaustion(t *testing.T) {
	records := make([]model.Record, policy.ProductionLimits().MaximumLexicalCandidates+1)
	for index := range records {
		records[index] = testRecord(index, "noise", model.Definition, model.Verified)
		records[index].SearchTerms = []string{"noise"}
	}
	document := testRecord(len(records)-1, "Level One Markdown", model.Heading, model.Verified)
	document.Path = "markdown/record-00027.md"
	document.Language = "markdown"
	document.SourceType = "document"
	document.SearchTerms = []string{"markdown"}
	records[len(records)-1] = document
	request := searchRequest("markdwn")
	request.Operation = wire.SearchDocs
	response := Search(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{document.Identity}) {
		t.Fatalf("fuzzy document records = %#v, want %s", got, document.Identity)
	}
}

func TestSearchUsesFullPhraseFuzzyFallbackBeforeBroadIndividualTokens(t *testing.T) {
	records := make([]model.Record, policy.ProductionLimits().MaximumLexicalCandidates+1)
	for index := range records {
		records[index] = testRecord(index, fmt.Sprintf("Level One Markdown %d#chunk-1", index+1000), model.DocumentChunk, model.Verified)
		records[index].Language = "markdown"
		records[index].SourceType = "document"
	}
	target := testRecord(len(records)-1, "Level One Markdown 27#chunk-1", model.DocumentChunk, model.Verified)
	target.Language = "markdown"
	target.SourceType = "document"
	records[len(records)-1] = target
	request := searchRequest("level one markdown 27#chunk-2")
	request.Operation = wire.SearchDocs
	response := Search(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{target.Identity}) {
		t.Fatalf("full-phrase fuzzy records = %#v, want %s", got, target.Identity)
	}
}

func TestSearchAdmitsSubstringFromBoundedPersistedFrontier(t *testing.T) {
	record := testRecord(0, "pkg.ServiceWorker", model.Definition, model.Verified)
	record.SearchTerms = []string{"serviceworker"}
	response := Search(indexedSnapshot([]model.Record{record}), searchRequest("vice"), policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{record.Identity}) {
		t.Fatalf("substring records = %#v, want %s", got, record.Identity)
	}
	if response.Partial {
		t.Fatalf("complete one-record frontier reported partial: %#v", response)
	}
}

func TestSearchReportsPartialWhenSubstringFrontierCannotCoverSnapshot(t *testing.T) {
	records := make([]model.Record, policy.ProductionLimits().MaximumLexicalCandidates+1)
	for index := range records {
		records[index] = testRecord(index, "pkg.Alpha", model.Definition, model.Verified)
		records[index].SearchTerms = []string{"alpha"}
	}
	response := Search(indexedSnapshot(records), searchRequest("zzzz"), policy.ProductionLimits())
	if !response.Partial || len(response.Records) != 0 {
		t.Fatalf("truncated substring frontier response = %#v", response)
	}
}

func TestSearchKeepsExactPunctuationQualifiedNameWithoutEmptyShortAlias(t *testing.T) {
	record := testRecord(0, "---", model.Definition, model.Verified)
	record.SearchTerms = []string{"punctuation"}
	response := Search(indexedSnapshot([]model.Record{record}), searchRequest("---"), policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{record.Identity}) {
		t.Fatalf("punctuation qualified result = %#v", got)
	}
}

func TestSearchAppliesEveryRequestFilter(t *testing.T) {
	record := testRecord(0, "pkg.Service", model.Definition, model.Verified)
	record.Path, record.Language, record.SourceType = "allowed/service.go", "go", "source"
	snapshot := indexedSnapshot([]model.Record{record})
	request := searchRequest("service")
	request.Filters = wire.Filters{PathPrefixes: []string{"allowed/"}, Languages: []string{"go"}, SymbolKinds: []string{"definition"}, SourceTypes: []string{"source"}}
	if got := identities(Search(snapshot, request, policy.ProductionLimits()).Records); !reflect.DeepEqual(got, []string{record.Identity}) {
		t.Fatalf("filtered = %#v", got)
	}
	request.Filters.SourceTypes = []string{"document"}
	if got := Search(snapshot, request, policy.ProductionLimits()).Records; len(got) != 0 {
		t.Fatalf("mismatched filter = %#v", got)
	}
}

func TestSearchFormsOneBoundedUnionRatherThanSuppressingFallback(t *testing.T) {
	exact := testRecord(0, "pkg.Service", model.Definition, model.Verified)
	exact.SearchTerms = []string{"service"}
	prefix := testRecord(1, "pkg.ServiceWorker", model.Definition, model.Verified)
	prefix.SearchTerms = []string{"serviceworker"}
	snapshot := indexedSnapshot([]model.Record{exact, prefix})
	response := Search(snapshot, searchRequest("service"), policy.ProductionLimits())
	if got, want := identities(response.Records), []string{exact.Identity, prefix.Identity}; !reflect.DeepEqual(got, want) {
		t.Fatalf("union = %#v, want %#v", got, want)
	}
}

func TestSearchCountsDuplicatePostingVisitsAgainstTheWorkCeiling(t *testing.T) {
	record := testRecord(0, "pkg.Service", model.Definition, model.Verified)
	record.SearchTerms = []string{"pkg.service", "service"}
	snapshot := indexedSnapshot([]model.Record{record})
	response := Search(snapshot, searchRequest("pkg.Service"), policy.ProductionLimits())
	if response.Counters.ConsideredRecords < 2 || response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates {
		t.Fatalf("duplicate visits = %#v", response.Counters)
	}
}

func TestSearchUsesFilterFacetBeforeLargeIrrelevantPostingPrefix(t *testing.T) {
	records := make([]model.Record, 4097)
	for index := range records {
		records[index] = testRecord(index, "pkg.Service", model.Definition, model.Verified)
		records[index].Language = "python"
		records[index].SearchTerms = []string{"service"}
	}
	hit := records[len(records)-1]
	hit.Language = "go"
	records[len(records)-1] = hit
	request := searchRequest("service")
	request.Filters.Languages = []string{"go"}
	response := Search(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
		t.Fatalf("filtered hit = %#v, want %s", got, hit.Identity)
	}
	if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.TermVisits > policy.ProductionLimits().MaximumFuzzyTerms {
		t.Fatalf("unbounded response = %#v", response)
	}
}

func TestSearchIntersectsAllCompoundFiltersBeforeLexicalAdmission(t *testing.T) {
	const half = 2048
	records := make([]model.Record, half*2+1)
	for index := 0; index < half; index++ {
		records[index] = testRecord(index, "pkg.Service", model.Definition, model.Verified)
		records[index].Path = fmt.Sprintf("blocked/%05d.go", index)
		records[index].Language = "go"
		records[index].SearchTerms = []string{"service"}
	}
	for index := half; index < half*2; index++ {
		records[index] = testRecord(index, "pkg.Service", model.Definition, model.Verified)
		records[index].Path = fmt.Sprintf("allowed/%05d.go", index)
		records[index].Language = "python"
		records[index].SearchTerms = []string{"service"}
	}
	hit := testRecord(half*2, "pkg.ServiceWorker", model.Definition, model.Verified)
	hit.Path, hit.Language, hit.SearchTerms = "allowed/hit.go", "go", []string{"serviceworker"}
	records[len(records)-1] = hit

	request := searchRequest("service")
	request.Filters.PathPrefixes = []string{"allowed/"}
	request.Filters.Languages = []string{"go"}
	response := Search(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
		t.Fatalf("compound-filter records = %#v, want %s (response=%#v)", got, hit.Identity, response)
	}
	if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.TermVisits > policy.ProductionLimits().MaximumFuzzyTerms {
		t.Fatalf("compound-filter work exceeded policy: %#v", response)
	}
}

func TestSearchChargesRankingMaterializationAndComparisons(t *testing.T) {
	records := make([]model.Record, 4)
	for index := range records {
		records[index] = testRecord(index, "", model.Definition, model.Verified)
		records[index].SearchTerms = []string{"service"}
	}
	response := Search(indexedSnapshot(records), searchRequest("service"), policy.ProductionLimits())
	if response.Counters.ConsideredRecords <= len(records) {
		t.Fatalf("ranking work is invisible in counters: %#v", response)
	}
	if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates {
		t.Fatalf("ranking work exceeded policy: %#v", response)
	}
}

func TestSearchFindsMaximumLengthQualifiedAndShortNamesOutsideFallbackFrontier(t *testing.T) {
	records := make([]model.Record, 4097)
	for index := 0; index < len(records)-1; index++ {
		records[index] = testRecord(index, "noise", model.Definition, model.Verified)
	}
	qualified := strings.Repeat("q", 510) + ".X"
	hit := testRecord(len(records)-1, qualified, model.Definition, model.Verified)
	hit.SearchTerms = []string{"unrelated"}
	records[len(records)-1] = hit
	for _, queryText := range []string{qualified, "x"} {
		response := Search(indexedSnapshot(records), searchRequest(queryText), policy.ProductionLimits())
		if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
			t.Fatalf("%d-byte exact query = %#v, want %s", len(queryText), got, hit.Identity)
		}
	}
}

func TestSearchFindsMaximumLengthHeadingOutsideFallbackFrontier(t *testing.T) {
	records := make([]model.Record, 4097)
	for index := 0; index < len(records)-1; index++ {
		records[index] = testRecord(index, "noise", model.Heading, model.Verified)
		records[index].Language, records[index].SourceType = "markdown", "document"
	}
	qualified := strings.Repeat("h", 512)
	hit := testRecord(len(records)-1, qualified, model.Heading, model.Verified)
	hit.Language, hit.SourceType, hit.SearchTerms = "markdown", "document", []string{"unrelated"}
	records[len(records)-1] = hit
	request := searchRequest(qualified)
	request.Operation = wire.SearchDocs
	response := Search(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
		t.Fatalf("maximum heading = %#v, want %s", got, hit.Identity)
	}
}

func TestRepositoryMapPersistsVerifiedSourceFirstGroupsAndTruthfulUnknownOmissions(t *testing.T) {
	records := make([]model.Record, 0, 4099)
	inferredModule := testRecord(0, "pkg", model.Module, model.Inferred)
	inferredModule.Path = "a/pkg.go"
	verifiedDefinition := testRecord(1, "pkg.Service", model.Definition, model.Verified)
	verifiedDefinition.Path = "a/pkg.go"
	records = append(records, inferredModule, verifiedDefinition)
	for index := 2; index < 4099; index++ {
		record := testRecord(index, fmt.Sprintf("Module%d", index), model.Module, model.Verified)
		record.Path = fmt.Sprintf("z/%05d.go", index)
		records = append(records, record)
	}
	request := mapRequest()
	response := RepositoryMap(indexedSnapshot(records), request, policy.ProductionLimits())
	if len(response.Records) == 0 || response.Records[0].Identity != verifiedDefinition.Identity {
		t.Fatalf("map representative = %#v, want verified source %s", response.Records, verifiedDefinition.Identity)
	}
	if !response.Partial || response.Omitted <= 0 || response.Omitted > policy.ProductionLimits().MaximumLexicalCandidates-request.MaximumResults {
		t.Fatalf("map omission semantics = %#v", response)
	}
}

func TestRepositoryMapFilterFacetFindsGroupBeyondPersistedFrontier(t *testing.T) {
	records := make([]model.Record, 4097)
	for index := range records {
		records[index] = testRecord(index, fmt.Sprintf("Module%d", index), model.Module, model.Verified)
		records[index].Path = fmt.Sprintf("pkg/%05d.go", index)
		records[index].Language = "python"
	}
	hit := records[len(records)-1]
	hit.Language = "go"
	records[len(records)-1] = hit
	request := mapRequest()
	request.Filters.Languages = []string{"go"}
	response := RepositoryMap(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
		t.Fatalf("filtered map = %#v, want %s", got, hit.Identity)
	}
}

func TestRepositoryMapIntersectsAllCompoundFiltersBeforeGrouping(t *testing.T) {
	const half = 2048
	records := make([]model.Record, half*2+1)
	for index := 0; index < half; index++ {
		records[index] = testRecord(index, fmt.Sprintf("Module%d", index), model.Module, model.Verified)
		records[index].Path, records[index].Language = fmt.Sprintf("blocked/%05d.go", index), "go"
	}
	for index := half; index < half*2; index++ {
		records[index] = testRecord(index, fmt.Sprintf("Module%d", index), model.Module, model.Verified)
		records[index].Path, records[index].Language = fmt.Sprintf("allowed/%05d.go", index), "python"
	}
	hit := testRecord(half*2, "Hit", model.Module, model.Verified)
	hit.Path, hit.Language = "allowed/hit.go", "go"
	records[len(records)-1] = hit
	request := mapRequest()
	request.Filters.PathPrefixes = []string{"allowed/"}
	request.Filters.Languages = []string{"go"}
	response := RepositoryMap(indexedSnapshot(records), request, policy.ProductionLimits())
	if got := identities(response.Records); !reflect.DeepEqual(got, []string{hit.Identity}) {
		t.Fatalf("compound-filter map = %#v, want %s (response=%#v)", got, hit.Identity, response)
	}
	if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates {
		t.Fatalf("compound-filter map exceeded policy: %#v", response)
	}
}

func TestCompoundFilterValuePermutationDoesNotChangeSearchOrMap(t *testing.T) {
	records := []model.Record{
		testRecord(0, "pkg.Service", model.Definition, model.Verified),
		testRecord(1, "pkg.ServiceWorker", model.Module, model.Verified),
		testRecord(2, "pkg.ServiceDoc", model.Heading, model.Verified),
	}
	records[0].Path, records[0].Language = "a/service.go", "go"
	records[1].Path, records[1].Language = "b/worker.py", "python"
	records[2].Path, records[2].Language, records[2].SourceType = "c/doc.md", "markdown", "document"
	snapshot := indexedSnapshot(records)
	left := searchRequest("service")
	left.Filters.Languages = []string{"go", "python"}
	left.Filters.PathPrefixes = []string{"a/", "b/"}
	right := left
	right.Filters.Languages = []string{"python", "go"}
	right.Filters.PathPrefixes = []string{"b/", "a/"}
	if first, second := Search(snapshot, left, policy.ProductionLimits()), Search(snapshot, right, policy.ProductionLimits()); !reflect.DeepEqual(first, second) {
		t.Fatalf("filter permutation changed search:\nleft=%#v\nright=%#v", first, second)
	}
	left.Operation, right.Operation = wire.RepositoryMap, wire.RepositoryMap
	if first, second := RepositoryMap(snapshot, left, policy.ProductionLimits()), RepositoryMap(snapshot, right, policy.ProductionLimits()); !reflect.DeepEqual(first, second) {
		t.Fatalf("filter permutation changed map:\nleft=%#v\nright=%#v", first, second)
	}
}

func TestRepositoryMapIsPermutationInvariantAndRenameEquivariant(t *testing.T) {
	records := []model.Record{
		testRecord(0, "Alpha", model.Module, model.Verified),
		testRecord(1, "Beta", model.Module, model.Verified),
	}
	records[0].Path, records[1].Path = "a/alpha.go", "b/beta.go"
	permuted := []model.Record{records[1], records[0]}
	left := RepositoryMap(indexedSnapshot(records), mapRequest(), policy.ProductionLimits())
	right := RepositoryMap(indexedSnapshot(permuted), mapRequest(), policy.ProductionLimits())
	if !reflect.DeepEqual(identities(left.Records), identities(right.Records)) {
		t.Fatalf("permutation changed map: %#v %#v", left, right)
	}
	renamed := append([]model.Record(nil), records...)
	for index := range renamed {
		renamed[index].Path = "renamed/" + renamed[index].Path
	}
	renameResponse := RepositoryMap(indexedSnapshot(renamed), mapRequest(), policy.ProductionLimits())
	for index := range left.Records {
		if renameResponse.Records[index].Identity != left.Records[index].Identity || renameResponse.Records[index].Path != "renamed/"+left.Records[index].Path {
			t.Fatalf("rename response = %#v, base = %#v", renameResponse, left)
		}
	}
}

func TestEditDistanceUsesUnicodeCodePoints(t *testing.T) {
	if got := editDistanceAtMost("café", "cafe", 2); got != 1 {
		t.Fatalf("accent distance = %d, want 1", got)
	}
	if got := editDistanceAtMost("東京", "京", 2); got != 1 {
		t.Fatalf("multibyte distance = %d, want 1", got)
	}
}

func FuzzUnicodeNormalizationAndDistanceBounded(f *testing.F) {
	f.Add("café", "cafe")
	f.Add("東京", "京")
	f.Fuzz(func(t *testing.T, left, right string) {
		for _, value := range []string{left, right} {
			normalized := normalize(value)
			if normalize(normalized) != normalized || strings.TrimSpace(normalized) != normalized {
				t.Fatalf("normalization is not idempotent for %q: %q", value, normalized)
			}
			for _, token := range tokens(value) {
				if token == "" || normalize(token) != token {
					t.Fatalf("non-canonical token %q from %q", token, value)
				}
			}
		}
		distance := editDistanceAtMost(left, right, 2)
		if distance < 0 || distance > 3 || distance != editDistanceAtMost(right, left, 2) {
			t.Fatalf("distance %q %q = %d", left, right, distance)
		}
	})
}

func FuzzComparatorTransitivity(f *testing.F) {
	f.Add("a.go", "b.go", "c.go")
	f.Fuzz(func(t *testing.T, a, b, c string) {
		records := []model.Record{
			testRecord(0, a, model.Module, model.Verified),
			testRecord(1, b, model.Module, model.Verified),
			testRecord(2, c, model.Module, model.Verified),
		}
		for index := range records {
			records[index].Path = []string{a, b, c}[index]
		}
		ab, bc, ac := compareRepresentative(records[0], records[1]), compareRepresentative(records[1], records[2]), compareRepresentative(records[0], records[2])
		if ab <= 0 && bc <= 0 && ac > 0 {
			t.Fatalf("non-transitive comparator: ab=%d bc=%d ac=%d", ab, bc, ac)
		}
		if sign(ab) != -sign(compareRepresentative(records[1], records[0])) {
			t.Fatalf("non-antisymmetric comparator: ab=%d ba=%d", ab, compareRepresentative(records[1], records[0]))
		}
		ranked := []rankedCandidate{newRankedCandidate(records[0], 3), newRankedCandidate(records[1], 3), newRankedCandidate(records[2], 3)}
		ab, bc, ac = compareRankedCandidate(ranked[0], ranked[1]), compareRankedCandidate(ranked[1], ranked[2]), compareRankedCandidate(ranked[0], ranked[2])
		if ab <= 0 && bc <= 0 && ac > 0 {
			t.Fatalf("non-transitive ranked comparator: ab=%d bc=%d ac=%d", ab, bc, ac)
		}
		if sign(ab) != -sign(compareRankedCandidate(ranked[1], ranked[0])) {
			t.Fatalf("non-antisymmetric ranked comparator: ab=%d ba=%d", ab, compareRankedCandidate(ranked[1], ranked[0]))
		}
	})
}

func FuzzPermutationAndUnionInvariance(f *testing.F) {
	f.Add("z/service.go", "a/worker.go", true)
	f.Fuzz(func(t *testing.T, firstPath, secondPath string, reverse bool) {
		first := testRecord(0, "pkg.Service", model.Definition, model.Verified)
		first.Path, first.SearchTerms = firstPath, []string{"service"}
		second := testRecord(1, "pkg.ServiceWorker", model.Definition, model.Verified)
		second.Path, second.SearchTerms = secondPath, []string{"serviceworker"}
		combined := []model.Record{first, second}
		if reverse {
			combined[0], combined[1] = combined[1], combined[0]
		}
		request := searchRequest("service")
		got := Search(indexedSnapshot(combined), request, policy.ProductionLimits())
		permuted := []model.Record{combined[1], combined[0]}
		if other := Search(indexedSnapshot(permuted), request, policy.ProductionLimits()); !reflect.DeepEqual(got, other) {
			t.Fatalf("permutation changed union:\nfirst=%#v\nsecond=%#v", got, other)
		}
		left := Search(indexedSnapshot([]model.Record{first}), request, policy.ProductionLimits())
		right := Search(indexedSnapshot([]model.Record{second}), request, policy.ProductionLimits())
		union := make(map[string]struct{}, len(left.Records)+len(right.Records))
		for _, record := range append(append([]model.Record(nil), left.Records...), right.Records...) {
			union[record.Identity] = struct{}{}
		}
		if len(got.Records) != len(union) {
			t.Fatalf("union changed result count: combined=%#v split=%#v", identities(got.Records), union)
		}
		for _, identity := range identities(got.Records) {
			if _, exists := union[identity]; !exists {
				t.Fatalf("union changed results: combined=%#v split=%#v", identities(got.Records), union)
			}
		}
	})
}

func FuzzMalformedSnapshotBounded(f *testing.F) {
	f.Add("service", "pkg/service.go")
	f.Fuzz(func(t *testing.T, term, path string) {
		record := testRecord(0, term, model.Definition, model.Verified)
		record.Path = path
		queryIndex := store.BuildQueryIndex([]model.Record{record})
		for _, snapshot := range []store.Snapshot{{Records: []model.Record{record}, Query: queryIndex}, {Query: queryIndex}} {
			response := Search(snapshot, searchRequest(term), policy.ProductionLimits())
			if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.TermVisits > policy.ProductionLimits().MaximumFuzzyTerms {
				t.Fatalf("unbounded response %#v", response)
			}
		}
	})
}

func FuzzCompoundFilterPermutationAndAccounting(f *testing.F) {
	f.Add("service.go", "worker.py", true)
	f.Fuzz(func(t *testing.T, firstSuffix, secondSuffix string, reverse bool) {
		first := testRecord(0, "pkg.Service", model.Definition, model.Verified)
		first.Path, first.Language = "a/"+firstSuffix, "go"
		second := testRecord(1, "pkg.ServiceWorker", model.Module, model.Verified)
		second.Path, second.Language = "b/"+secondSuffix, "python"
		snapshot := indexedSnapshot([]model.Record{first, second})
		request := searchRequest("service")
		request.Filters.Languages = []string{"go", "python"}
		request.Filters.PathPrefixes = []string{"a/", "b/"}
		permuted := request
		permuted.Filters.Languages = []string{"python", "go"}
		permuted.Filters.PathPrefixes = []string{"b/", "a/"}
		if reverse {
			request, permuted = permuted, request
		}
		left, right := Search(snapshot, request, policy.ProductionLimits()), Search(snapshot, permuted, policy.ProductionLimits())
		if !reflect.DeepEqual(left, right) {
			t.Fatalf("compound filter permutation changed search:\nleft=%#v\nright=%#v", left, right)
		}
		for _, response := range []Response{left, right} {
			if response.Counters.ConsideredRecords > policy.ProductionLimits().MaximumLexicalCandidates || response.TermVisits > policy.ProductionLimits().MaximumFuzzyTerms {
				t.Fatalf("unbounded compound response: %#v", response)
			}
		}
		request.Operation, permuted.Operation = wire.RepositoryMap, wire.RepositoryMap
		if leftMap, rightMap := RepositoryMap(snapshot, request, policy.ProductionLimits()), RepositoryMap(snapshot, permuted, policy.ProductionLimits()); !reflect.DeepEqual(leftMap, rightMap) {
			t.Fatalf("compound filter permutation changed map:\nleft=%#v\nright=%#v", leftMap, rightMap)
		}
	})
}

func sign(value int) int {
	if value < 0 {
		return -1
	}
	if value > 0 {
		return 1
	}
	return 0
}

func searchRequest(text string) wire.Request {
	return wire.Request{Operation: wire.SearchSymbols, Query: &text, MaximumResults: 64, Filters: wire.Filters{}}
}

func mapRequest() wire.Request {
	return wire.Request{Operation: wire.RepositoryMap, MaximumResults: 64, Filters: wire.Filters{}}
}

func testRecord(index int, name string, kind model.RecordKind, evidence model.EvidenceClass) model.Record {
	return model.Record{Identity: fmt.Sprintf("record-%05d", index), Path: fmt.Sprintf("pkg/%05d.go", index), StartLine: 1, EndLine: 1, Language: "go", RecordKind: kind, SourceType: "source", QualifiedName: name, ExtractionMethod: "test", EvidenceClass: evidence, SearchTerms: []string{"service"}}
}

func identities(records []model.Record) []string {
	result := make([]string, len(records))
	for index, record := range records {
		result[index] = record.Identity
	}
	return result
}

func indexedSnapshot(records []model.Record) store.Snapshot {
	return store.Snapshot{Records: records, Query: store.BuildQueryIndex(records)}
}
