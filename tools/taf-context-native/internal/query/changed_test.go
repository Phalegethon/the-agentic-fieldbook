package query

import (
	"reflect"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// The fixture is one small repository: a Python module whose two definitions
// sit apart with an import and a use between them, a Go file carrying a module
// record and an entry point, and a Markdown file, so a test can watch exactly
// which record kinds an intersection admits.
func changedFixture() []model.Record {
	return []model.Record{
		pythonRecord("a-module", "pkg/a.py", "a", model.Module, 1, 30),
		pythonImport("a-import", "pkg/a.py", "load", "pkg.b", 2),
		pythonRecord("a-first", "pkg/a.py", "a.first", model.Definition, 5, 9),
		pythonReference("a-first-uses", "pkg/a.py", "a.first", 5, 9, []model.ReferenceEntry{
			{Name: "load", Line: 6, Count: 1},
		}),
		pythonRecord("a-second", "pkg/a.py", "a.second", model.Definition, 12, 20),
		goRecord("main-module", "cmd/main.go", "main", model.Module, 1, 12),
		goRecord("main-run", "cmd/main.go", "main.Run", model.EntryPoint, 4, 8),
		markdownHeading("readme-overview", "docs/readme.md", "Overview", 1, 4),
	}
}

func TestChangedAdmitsOnlyTheSymbolsAHunkTouches(t *testing.T) {
	snapshot := relatedSnapshot(changedFixture())
	response := Changed(snapshot, changedRequest(changedPath("pkg/a.py", [2]int{6, 7})), policy.ProductionLimits())
	if response.Partial || response.Omitted != 0 || response.Unindexed {
		t.Fatalf("response = %#v", response)
	}
	if got, want := identities(response.Records), []string{"a-module", "a-first"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("changed symbols = %#v, want %#v", got, want)
	}
}

// A hunk that only grazes a definition's first or last line still changed it,
// and a hunk between two definitions changed neither.
func TestChangedIncludesADefinitionTouchedOnlyAtItsBoundaryLine(t *testing.T) {
	for _, testCase := range []struct {
		name string
		span [2]int
		want []string
	}{
		{name: "last line", span: [2]int{9, 9}, want: []string{"a-module", "a-first"}},
		{name: "first line", span: [2]int{12, 12}, want: []string{"a-module", "a-second"}},
		{name: "between definitions", span: [2]int{10, 11}, want: []string{"a-module"}},
		{name: "after every definition", span: [2]int{25, 26}, want: []string{"a-module"}},
		{name: "outside the file", span: [2]int{40, 41}, want: []string{}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			response := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("pkg/a.py", testCase.span)), policy.ProductionLimits())
			if got := identities(response.Records); !reflect.DeepEqual(got, testCase.want) {
				t.Fatalf("span %v = %#v, want %#v", testCase.span, got, testCase.want)
			}
		})
	}
}

func TestChangedIntersectsEverySpanOfOnePath(t *testing.T) {
	response := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("pkg/a.py", [2]int{6, 6}, [2]int{13, 13})), policy.ProductionLimits())
	if got, want := identities(response.Records), []string{"a-module", "a-first", "a-second"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("changed symbols = %#v, want %#v", got, want)
	}
}

// An entry with no spans is a whole-file change: every definition, entry point,
// and module record of that path is changed, and nothing else ever is.
func TestChangedWholeFileEntryAdmitsEverySymbolOfThePath(t *testing.T) {
	response := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("cmd/main.go"), changedPath("pkg/a.py")), policy.ProductionLimits())
	if response.Partial || response.Omitted != 0 || response.Unindexed {
		t.Fatalf("response = %#v", response)
	}
	if got, want := identities(response.Records), []string{"main-module", "main-run", "a-module", "a-first", "a-second"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("changed symbols = %#v, want %#v", got, want)
	}
}

// Imports, uses of a name, and document records are never changed symbols. A
// path whose only records are of those kinds is indexed all the same, so it
// reports no findings and no warning flag.
func TestChangedExcludesImportsReferencesAndDocumentRecords(t *testing.T) {
	response := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("docs/readme.md")), policy.ProductionLimits())
	if len(response.Records) != 0 || response.Unindexed || response.Partial {
		t.Fatalf("document path = %#v", response)
	}
}

func TestChangedAppliesTheRequestFilters(t *testing.T) {
	request := changedRequest(changedPath("cmd/main.go"), changedPath("pkg/a.py"))
	request.Filters.Languages = []string{"go"}
	response := Changed(relatedSnapshot(changedFixture()), request, policy.ProductionLimits())
	if got, want := identities(response.Records), []string{"main-module", "main-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("language filter = %#v, want %#v", got, want)
	}
	kinds := changedRequest(changedPath("pkg/a.py"))
	kinds.Filters.SymbolKinds = []string{"definition"}
	if got, want := identities(Changed(relatedSnapshot(changedFixture()), kinds, policy.ProductionLimits()).Records), []string{"a-first", "a-second"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("kind filter = %#v, want %#v", got, want)
	}
}

// An inferred definition is hidden from a change set the way it is hidden from
// a search, and admitted only when the caller asked for inferred evidence.
func TestChangedHidesInferredRecordsUnlessTheyAreAllowed(t *testing.T) {
	records := changedFixtureWithInferred("a-second")
	if got, want := identities(Changed(relatedSnapshot(records), changedRequest(changedPath("pkg/a.py")), policy.ProductionLimits()).Records), []string{"a-module", "a-first"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("verified only = %#v, want %#v", got, want)
	}
	request := changedRequest(changedPath("pkg/a.py"))
	request.AllowInferred = true
	if got, want := identities(Changed(relatedSnapshot(records), request, policy.ProductionLimits()).Records), []string{"a-module", "a-first", "a-second"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("allow inferred = %#v, want %#v", got, want)
	}
}

// A ranking overflow is a counted omission, not an exhausted search.
func TestChangedRanksIntoMaximumResultsAndCountsTheRest(t *testing.T) {
	request := changedRequest(changedPath("pkg/a.py"))
	request.MaximumResults = 2
	response := Changed(relatedSnapshot(changedFixture()), request, policy.ProductionLimits())
	if response.Partial || response.Omitted != 1 {
		t.Fatalf("bounded response = %#v", response)
	}
	if got, want := identities(response.Records), []string{"a-module", "a-first"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("bounded records = %#v, want %#v", got, want)
	}
}

// A changed path the index carries no record for is not an omission: it sets
// one flag the engine turns into a single warning.
func TestChangedFlagsAChangedPathTheIndexDoesNotCarry(t *testing.T) {
	response := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("pkg/missing.py"), changedPath("cmd/main.go")), policy.ProductionLimits())
	if !response.Unindexed || response.Omitted != 0 || response.Partial {
		t.Fatalf("unindexed response = %#v", response)
	}
	if got, want := identities(response.Records), []string{"main-module", "main-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("records = %#v, want %#v", got, want)
	}
	// A path that differs from an indexed path only in letter case is a
	// different file to the engine, so it is reported as not indexed.
	cased := Changed(relatedSnapshot(changedFixture()), changedRequest(changedPath("pkg/A.py")), policy.ProductionLimits())
	if !cased.Unindexed || len(cased.Records) != 0 {
		t.Fatalf("case-variant path = %#v", cased)
	}
}

// The order is evidence-major, the house rule of boundedRanking: verified
// findings first, then path, then start line. The second case is what
// distinguishes that from a path-major order: an inferred definition of
// `cmd/main.go` sorts behind every verified finding of `pkg/a.py`, even though
// its own path comes first, so a truncated list is the strongest prefix rather
// than the alphabetically first one.
func TestChangedOrdersByEvidenceThenPathThenStartLineDeterministically(t *testing.T) {
	for _, testCase := range []struct {
		name          string
		records       []model.Record
		allowInferred bool
		want          []string
	}{
		{
			name:    "verified only",
			records: changedFixture(),
			want:    []string{"main-module", "main-run", "a-module", "a-first", "a-second"},
		},
		{
			name:          "an inferred finding follows every verified one",
			records:       changedFixtureWithInferred("main-run"),
			allowInferred: true,
			want:          []string{"main-module", "a-module", "a-first", "a-second", "main-run"},
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			request := changedRequest(changedPath("pkg/a.py"), changedPath("cmd/main.go"), changedPath("docs/readme.md"))
			request.AllowInferred = testCase.allowInferred
			for attempt := 0; attempt < 3; attempt++ {
				response := Changed(relatedSnapshot(testCase.records), request, policy.ProductionLimits())
				if got := identities(response.Records); !reflect.DeepEqual(got, testCase.want) {
					t.Fatalf("attempt %d = %#v, want %#v", attempt, got, testCase.want)
				}
			}
		})
	}
}

// Two findings of the same path, start line and kind fall through to the
// qualified name, so the order stays total when nothing structural separates
// them: `a.alpha` precedes `a.first`, which precedes `a.zulu`, whatever order
// the scan offered them in.
func TestChangedBreaksAPathAndLineTieByQualifiedName(t *testing.T) {
	records := append(changedFixture(),
		pythonRecord("a-tie-zulu", "pkg/a.py", "a.zulu", model.Definition, 5, 9),
		pythonRecord("a-tie-alpha", "pkg/a.py", "a.alpha", model.Definition, 5, 9),
	)
	response := Changed(relatedSnapshot(records), changedRequest(changedPath("pkg/a.py", [2]int{6, 6})), policy.ProductionLimits())
	want := []string{"a-module", "a-tie-alpha", "a-first", "a-tie-zulu"}
	if got := identities(response.Records); !reflect.DeepEqual(got, want) {
		t.Fatalf("tied findings = %#v, want %#v", got, want)
	}
}

// A scan the budget cut short proves nothing about the path it was scanning,
// so the unindexed report is guarded by `!partial`. The arithmetic of the cut:
// with no lexical ceiling of its own the work budget is four units per record,
// 32 for this eight-record fixture, and one unit is charged per record visited
// plus one per record offered to the ranking. Three whole-file entries of
// `pkg/a.py` (five records, three of them symbols) cost 8 each and one of
// `cmd/main.go` (two records, both symbols) costs 4, which is 28. The last
// entry names a path that differs from an indexed one only in letter case, so
// no record of its range matches its exact path, and the scan is cut after
// four more visits with the path still unproven.
func TestChangedDoesNotReportAnUnindexedPathWhenTheBudgetCutTheScan(t *testing.T) {
	request := changedRequest(
		changedPath("pkg/a.py"), changedPath("pkg/a.py"), changedPath("pkg/a.py"),
		changedPath("cmd/main.go"), changedPath("pkg/A.py"),
	)
	response := Changed(relatedSnapshot(changedFixture()), request, policy.Limits{})
	if !response.Partial || response.Unindexed {
		t.Fatalf("budget-cut scan = %#v, want partial without an unindexed report", response)
	}
	if response.Counters.ConsideredRecords != 32 {
		t.Fatalf("considered records = %d, want the whole 32-unit budget", response.Counters.ConsideredRecords)
	}
	// The same case-variant path is reported once the scan is allowed to
	// finish, so the silence above is the guard rather than a blind spot.
	full := Changed(relatedSnapshot(changedFixture()), request, policy.ProductionLimits())
	if full.Partial || !full.Unindexed {
		t.Fatalf("complete scan = %#v, want an unindexed report", full)
	}
}

// The intersection is charged to the shared work budget, so a path index that
// runs off the end of the record slice stops the scan and reports the result
// as partial instead of silently answering from a truncated store.
func TestChangedMarksPartialWhenThePathIndexRunsOffTheEnd(t *testing.T) {
	snapshot := changedTruncatedSnapshot(changedFixture(), 4)
	response := Changed(snapshot, changedRequest(changedPath("pkg/a.py"), changedPath("cmd/main.go")), policy.ProductionLimits())
	if !response.Partial {
		t.Fatalf("truncated snapshot = %#v, want a partial response", response)
	}
}

func TestChangedWithoutASelectorOrRecordsReturnsNothing(t *testing.T) {
	request := changedRequest()
	request.ChangedRanges = nil
	if response := Changed(relatedSnapshot(changedFixture()), request, policy.ProductionLimits()); response.Partial || len(response.Records) != 0 || response.Unindexed {
		t.Fatalf("missing selector = %#v", response)
	}
	if response := Changed(relatedSnapshot(nil), changedRequest(changedPath("pkg/a.py")), policy.ProductionLimits()); response.Partial || len(response.Records) != 0 {
		t.Fatalf("empty snapshot = %#v", response)
	}
	if response := Changed(relatedSnapshot(changedFixture()), changedRequest(), policy.ProductionLimits()); response.Partial || len(response.Records) != 0 || response.Unindexed {
		t.Fatalf("empty change set = %#v", response)
	}
}

// changedFixtureWithInferred is the fixture with one record downgraded to
// inferred evidence, so a test can watch what the evidence class alone changes.
func changedFixtureWithInferred(identity string) []model.Record {
	records := changedFixture()
	for index := range records {
		if records[index].Identity == identity {
			records[index].EvidenceClass = model.Inferred
		}
	}
	return records
}

func changedRequest(entries ...wire.ChangedRange) wire.Request {
	selector := append([]wire.ChangedRange(nil), entries...)
	return wire.Request{
		SchemaVersion: "3", Operation: wire.ChangedSymbols, ChangedRanges: &selector,
		MaximumResults: 64, Filters: wire.Filters{},
	}
}

func changedPath(path string, spans ...[2]int) wire.ChangedRange {
	return wire.ChangedRange{Path: path, Ranges: append([][2]int(nil), spans...)}
}

func markdownHeading(identity, path, name string, start, end int) model.Record {
	record := pythonRecord(identity, path, name, model.Heading, start, end)
	record.Language, record.ExtractionMethod, record.SourceType = "markdown", "markdown", "document"
	return record
}

// changedTruncatedSnapshot keeps the query index of the whole record set but
// hands the intersection a shorter record slice, so the path index runs off
// the end exactly as it does when a scan cannot finish.
func changedTruncatedSnapshot(records []model.Record, dropped int) store.Snapshot {
	full := relatedSnapshot(records)
	return store.Snapshot{Records: full.Records[:len(full.Records)-dropped], Query: full.Query}
}
