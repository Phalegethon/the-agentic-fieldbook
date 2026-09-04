package query

import (
	"fmt"
	"reflect"
	"sort"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// The fixture is one small Python package. Module a imports load from module b
// and calls it inside a.run together with a same-file helper; module c defines
// its own load and calls it, so the same written name resolves to two
// different definitions depending on the file it is read from.
func relatedFixture() []model.Record {
	return []model.Record{
		pythonRecord("a-module", "pkg/a.py", "a", model.Module, 1, 13),
		pythonImport("a-import-load", "pkg/a.py", "load", "pkg.b", 1),
		pythonRecord("a-run", "pkg/a.py", "a.run", model.Definition, 3, 9),
		pythonReference("a-run-uses", "pkg/a.py", "a.run", 3, 9, []model.ReferenceEntry{
			{Name: "load", Line: 5, Count: 2},
			{Name: "helper", Line: 8, Count: 1},
		}),
		pythonRecord("a-helper", "pkg/a.py", "a.helper", model.Definition, 11, 12),
		pythonRecord("b-module", "pkg/b.py", "b", model.Module, 1, 4),
		pythonRecord("b-load", "pkg/b.py", "b.load", model.Definition, 2, 4),
		pythonRecord("c-module", "pkg/c.py", "c", model.Module, 1, 8),
		pythonRecord("c-load", "pkg/c.py", "c.load", model.Definition, 2, 3),
		pythonRecord("c-main", "pkg/c.py", "c.main", model.Definition, 6, 8),
		pythonReference("c-main-uses", "pkg/c.py", "c.main", 6, 8, []model.ReferenceEntry{
			{Name: "load", Line: 7, Count: 1},
		}),
	}
}

func TestRelatedCallersResolveThroughTheImportAndIgnoreTheSameNameElsewhere(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	response := Related(snapshot, relatedRequest("callers", "b-load"), policy.ProductionLimits())
	if response.Unknown || response.Partial || response.Omitted != 0 {
		t.Fatalf("response = %#v", response)
	}
	if got, want := relatedIdentities(response.Findings), []string{"a-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers of b.load = %#v, want %#v", got, want)
	}
	finding := response.Findings[0]
	if finding.Relation != "call" || finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 5 || finding.ReferenceCount != 2 {
		t.Fatalf("edge = %#v", finding)
	}
	if finding.Record.RecordKind != model.Definition || finding.Record.QualifiedName != "a.run" {
		t.Fatalf("caller record = %#v", finding.Record)
	}
}

func TestRelatedCallersResolveWithinTheSameFile(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	response := Related(snapshot, relatedRequest("callers", "c-load"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"c-main"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers of c.load = %#v, want %#v", got, want)
	}
	if finding := response.Findings[0]; finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 7 || finding.ReferenceCount != 1 {
		t.Fatalf("edge = %#v", finding)
	}
}

func TestRelatedCalleesRankVerifiedEdgesInPathOrder(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	response := Related(snapshot, relatedRequest("callees", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"a-helper", "b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callees of a.run = %#v, want %#v", got, want)
	}
	for _, finding := range response.Findings {
		if finding.Relation != "call" || finding.EdgeEvidence != model.Verified {
			t.Fatalf("callee edge = %#v", finding)
		}
	}
	if line, count := response.Findings[0].ReferenceLine, response.Findings[0].ReferenceCount; line != 8 || count != 1 {
		t.Fatalf("a.helper edge = %d:%d, want 8:1", line, count)
	}
	if line, count := response.Findings[1].ReferenceLine, response.Findings[1].ReferenceCount; line != 5 || count != 2 {
		t.Fatalf("b.load edge = %d:%d, want 5:2", line, count)
	}
}

// Without the import, load is only a name, so every definition that carries it
// - including the one module c defines for itself - is a candidate and no edge
// may claim to be verified.
func TestRelatedCalleesHideNameOnlyCandidatesUnlessInferredIsAllowed(t *testing.T) {
	records := make([]model.Record, 0, 13)
	for _, record := range relatedFixture() {
		if record.Identity == "a-import-load" {
			continue
		}
		records = append(records, record)
	}
	records = append(records,
		pythonRecord("d-module", "pkg/d.py", "d", model.Module, 1, 4),
		pythonRecord("d-load", "pkg/d.py", "d.load", model.Definition, 2, 4),
	)
	snapshot := relatedSnapshot(records)

	verified := Related(snapshot, relatedRequest("callees", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(verified.Findings), []string{"a-helper"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("default callees = %#v, want %#v", got, want)
	}
	request := relatedRequest("callees", "a-run")
	request.AllowInferred = true
	inferred := Related(snapshot, request, policy.ProductionLimits())
	if got, want := relatedIdentities(inferred.Findings), []string{"a-helper", "b-load", "c-load", "d-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("inferred callees = %#v, want %#v", got, want)
	}
	for _, finding := range inferred.Findings[1:] {
		if finding.EdgeEvidence != model.Inferred || finding.ReferenceLine != 5 {
			t.Fatalf("name-only edge = %#v", finding)
		}
	}
}

func TestRelatedImportersReturnTheImportRecordsThemselves(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	for _, anchor := range []string{"b-module", "b-load"} {
		response := Related(snapshot, relatedRequest("importers", anchor), policy.ProductionLimits())
		if got, want := relatedIdentities(response.Findings), []string{"a-import-load"}; !reflect.DeepEqual(got, want) {
			t.Fatalf("importers of %s = %#v, want %#v", anchor, got, want)
		}
		finding := response.Findings[0]
		if finding.Relation != "import" || finding.EdgeEvidence != model.Verified || finding.Record.RecordKind != model.Import {
			t.Fatalf("importer edge for %s = %#v", anchor, finding)
		}
		if finding.ReferenceLine != 1 {
			t.Fatalf("importer line for %s = %d, want 1", anchor, finding.ReferenceLine)
		}
	}
}

func TestRelatedImportsResolveASingleNameImportToItsDefinition(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	response := Related(snapshot, relatedRequest("imports", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("imports of a.run = %#v, want %#v", got, want)
	}
	if finding := response.Findings[0]; finding.Relation != "import" || finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 1 || finding.ReferenceCount != 1 {
		t.Fatalf("import edge = %#v", finding)
	}
}

func TestRelatedAppliesRequestFiltersToTheRelatedRecords(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	request := relatedRequest("callees", "a-run")
	request.Filters.PathPrefixes = []string{"pkg/b"}
	response := Related(snapshot, request, policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("filtered callees = %#v, want %#v", got, want)
	}
}

func TestRelatedBoundsResultsAndCountsOmissions(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	request := relatedRequest("callees", "a-run")
	request.MaximumResults = 1
	response := Related(snapshot, request, policy.ProductionLimits())
	if len(response.Findings) != 1 || response.Omitted != 1 || response.Partial {
		t.Fatalf("bounded callees = %#v", response)
	}
	if response.Findings[0].Record.Identity != "a-helper" {
		t.Fatalf("kept finding = %#v", response.Findings[0])
	}
}

func TestRelatedRefusesAnchorsThatAreNotDefinitionsModulesOrEntryPoints(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	for _, anchor := range []string{"a-run-uses", "a-import-load", "missing"} {
		response := Related(snapshot, relatedRequest("callers", anchor), policy.ProductionLimits())
		if !response.Unknown || len(response.Findings) != 0 {
			t.Fatalf("anchor %s = %#v", anchor, response)
		}
	}
}

func TestRelatedIsDeterministic(t *testing.T) {
	snapshot := relatedSnapshot(relatedFixture())
	for _, direction := range []string{"callers", "callees", "importers", "imports"} {
		request := relatedRequest(direction, "a-run", "b-load")
		request.AllowInferred = true
		first := Related(snapshot, request, policy.ProductionLimits())
		second := Related(snapshot, request, policy.ProductionLimits())
		if !reflect.DeepEqual(first, second) {
			t.Fatalf("%s is not deterministic:\nfirst=%#v\nsecond=%#v", direction, first, second)
		}
	}
}

// A module-level call in a tree-sitter language has no module record to point
// at, so the caller is synthesized from the reference record itself.
func TestRelatedSynthesizesTheModuleHostOfAFileLevelCall(t *testing.T) {
	records := []model.Record{
		pythonRecord("b-module", "pkg/b.py", "b", model.Module, 1, 4),
		pythonRecord("b-load", "pkg/b.py", "b.load", model.Definition, 2, 4),
		pythonImport("e-import-load", "pkg/e.py", "load", "pkg.b", 1),
		pythonReference("e-uses", "pkg/e.py", "e", 1, 5, []model.ReferenceEntry{{Name: "load", Line: 4, Count: 1}}),
	}
	response := Related(relatedSnapshot(records), relatedRequest("callers", "b-load"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"e-uses"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("module-level callers = %#v, want %#v", got, want)
	}
	host := response.Findings[0].Record
	if host.RecordKind != model.Module || host.QualifiedName != "e" || host.Path != "pkg/e.py" {
		t.Fatalf("synthesized host = %#v", host)
	}
	if host.StartLine != 4 || host.EndLine != 4 || host.Preview != "" || host.TargetName != "" || host.ReferenceCount != 0 {
		t.Fatalf("synthesized host shape = %#v", host)
	}
	if finding := response.Findings[0]; finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 4 || finding.ReferenceCount != 1 {
		t.Fatalf("module-level edge = %#v", finding)
	}
}

// A Go package spans the files of one directory, so a call in one file
// resolves to the definition another file of the same package carries. A file
// that happens to carry the same module name in another directory never joins
// that scope.
func TestRelatedCallersResolveAcrossTheFilesOfOnePackage(t *testing.T) {
	records := []model.Record{
		goRecord("svc-caller-module", "svc/caller.go", "sample", model.Module, 1, 1),
		goRecord("svc-caller-main", "svc/caller.go", "sample.Main", model.Definition, 3, 5),
		goReference("svc-caller-uses", "svc/caller.go", "sample.Main", 3, 5, []model.ReferenceEntry{{Name: "helper", Line: 4, Count: 1}}),
		goRecord("svc-helper-module", "svc/helper.go", "sample", model.Module, 1, 1),
		goRecord("svc-helper", "svc/helper.go", "sample.helper", model.Definition, 3, 4),
		goRecord("tool-helper-module", "tool/helper.go", "sample", model.Module, 1, 1),
		goRecord("tool-helper", "tool/helper.go", "sample.helper", model.Definition, 3, 4),
	}
	snapshot := relatedSnapshot(records)

	response := Related(snapshot, relatedRequest("callers", "svc-helper"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"svc-caller-main"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers inside the package = %#v, want %#v", got, want)
	}
	if finding := response.Findings[0]; finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 4 {
		t.Fatalf("package edge = %#v", finding)
	}
	elsewhere := Related(snapshot, relatedRequest("callers", "tool-helper"), policy.ProductionLimits())
	if len(elsewhere.Findings) != 0 {
		t.Fatalf("callers of the unrelated package = %#v", elsewhere.Findings)
	}
}

// A Go package carries one module record per file, so an import of it names a
// directory rather than a single record: the module resolves as long as the
// name belongs to one directory, and the package's first file stands for it.
func TestRelatedResolvesAnImportedGoPackageAcrossItsFiles(t *testing.T) {
	records := []model.Record{
		goRecord("store-one-module", "store/one.go", "store", model.Module, 1, 1),
		goRecord("store-load", "store/one.go", "store.Load", model.Definition, 3, 6),
		goRecord("store-two-module", "store/two.go", "store", model.Module, 1, 1),
		goRecord("app-module", "app/main.go", "app", model.Module, 1, 1),
		goImport("app-import-store", "app/main.go", "store", "example.com/x/store", 3),
		goRecord("app-run", "app/main.go", "app.Run", model.Definition, 5, 8),
		goReference("app-run-uses", "app/main.go", "app.Run", 5, 8, []model.ReferenceEntry{{Name: "store.Load", Line: 6, Count: 1}}),
	}
	snapshot := relatedSnapshot(records)

	callers := Related(snapshot, relatedRequest("callers", "store-load"), policy.ProductionLimits())
	if got, want := relatedIdentities(callers.Findings), []string{"app-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers across packages = %#v, want %#v", got, want)
	}
	if callers.Findings[0].EdgeEvidence != model.Verified {
		t.Fatalf("cross-package edge = %#v", callers.Findings[0])
	}
	imports := Related(snapshot, relatedRequest("imports", "app-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(imports.Findings), []string{"store-one-module"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("imports = %#v, want the package's first file %#v", got, want)
	}
	if finding := imports.Findings[0]; finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 3 {
		t.Fatalf("import edge = %#v", finding)
	}
	for _, anchor := range []string{"store-one-module", "store-two-module"} {
		importers := Related(snapshot, relatedRequest("importers", anchor), policy.ProductionLimits())
		if got, want := relatedIdentities(importers.Findings), []string{"app-import-store"}; !reflect.DeepEqual(got, want) {
			t.Fatalf("importers of %s = %#v, want %#v", anchor, got, want)
		}
		if importers.Findings[0].EdgeEvidence != model.Verified {
			t.Fatalf("importers of %s edge = %#v", anchor, importers.Findings[0])
		}
	}
}

func relatedRequest(direction string, identities ...string) wire.Request {
	sorted := append([]string(nil), identities...)
	sort.Strings(sorted)
	return wire.Request{
		SchemaVersion: "2", Operation: wire.RelatedSymbols, Direction: &direction,
		ResultIdentities: sorted, MaximumResults: 64, Filters: wire.Filters{},
	}
}

func relatedIdentities(findings []RelatedFinding) []string {
	output := make([]string, len(findings))
	for index, finding := range findings {
		output[index] = finding.Record.Identity
	}
	return output
}

// relatedSnapshot mirrors what the engine hands the query package: records
// sorted by identity, with the query index derived from them.
func relatedSnapshot(records []model.Record) store.Snapshot {
	sorted := append([]model.Record(nil), records...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Identity < sorted[j].Identity })
	return store.Snapshot{Records: sorted, Query: store.BuildQueryIndex(sorted)}
}

func pythonRecord(identity, path, name string, kind model.RecordKind, start, end int) model.Record {
	return model.Record{
		Identity: identity, Path: path, StartLine: start, EndLine: end, Language: "python",
		RecordKind: kind, SourceType: "source", QualifiedName: name,
		ExtractionMethod: "tree-sitter-python", EvidenceClass: model.Verified,
	}
}

func pythonImport(identity, path, bound, specifier string, line int) model.Record {
	record := pythonRecord(identity, path, bound, model.Import, line, line)
	record.TargetName = specifier
	return record
}

func goRecord(identity, path, name string, kind model.RecordKind, start, end int) model.Record {
	record := pythonRecord(identity, path, name, kind, start, end)
	record.Language, record.ExtractionMethod = "go", "go/ast"
	return record
}

func goImport(identity, path, bound, specifier string, line int) model.Record {
	record := goRecord(identity, path, bound, model.Import, line, line)
	record.TargetName = specifier
	return record
}

func goReference(identity, path, host string, start, end int, entries []model.ReferenceEntry) model.Record {
	record := pythonReference(identity, path, host, start, end, entries)
	record.Language, record.ExtractionMethod = "go", "go/ast"
	return record
}

func pythonReference(identity, path, host string, start, end int, entries []model.ReferenceEntry) model.Record {
	record := pythonRecord(identity, path, host, model.Reference, start, end)
	record.TargetName = model.FormatReferenceTable(entries)
	for _, entry := range entries {
		record.ReferenceCount += entry.Count
		record.SearchTerms = append(record.SearchTerms, normalize(entry.Name))
	}
	return record
}

// An exhausted work budget must not empty the result: the ranking is charged as
// each edge is resolved, the way Search charges admission, so what was already
// resolved survives and only the unscanned remainder is reported as partial.
func TestRelatedKeepsResolvedEdgesWhenTheWorkBudgetRunsOut(t *testing.T) {
	const callerCount = 26
	records := []model.Record{
		pythonRecord("m-module", "pkg/m.py", "m", model.Module, 1, 4),
		pythonRecord("m-load", "pkg/m.py", "m.load", model.Definition, 2, 4),
	}
	records = append(records, pythonImport("c-import", "pkg/c.py", "load", "pkg.m", 1))
	for index := 0; index < callerCount; index++ {
		name := fmt.Sprintf("c.f%02d", index)
		start := 3 + index*3
		records = append(records,
			pythonRecord(fmt.Sprintf("c-f%02d", index), "pkg/c.py", name, model.Definition, start, start+2),
			pythonReference(fmt.Sprintf("c-f%02d-uses", index), "pkg/c.py", name, start, start+2,
				[]model.ReferenceEntry{{Name: "load", Line: start + 1, Count: 1}}),
		)
	}
	snapshot := relatedSnapshot(records)
	request := relatedRequest("callers", "m-load")

	full := Related(snapshot, request, policy.ProductionLimits())
	if len(full.Findings) != callerCount || full.Partial {
		t.Fatalf("unbounded callers = %d findings partial=%v, want %d and false", len(full.Findings), full.Partial, callerCount)
	}

	counts := make([]int, 0, 256)
	best, bestBudget := 0, 0
	for budget := 25; budget <= 20000; budget += 25 {
		response := Related(snapshot, request, relatedLimits(budget))
		counts = append(counts, len(response.Findings))
		if size := len(counts); size > 1 && counts[size-1] < counts[size-2] {
			t.Fatalf("findings dropped as the budget grew, at %d: %v", budget, counts)
		}
		if response.Partial && len(response.Findings) > best {
			best, bestBudget = len(response.Findings), budget
		}
	}
	if best < callerCount-2 {
		t.Fatalf("best partial result = %d findings at budget %d, want at least %d: %v", best, bestBudget, callerCount-2, counts)
	}
}

// Resolution never crosses languages. A Python file that imports a name whose
// only definition is Rust resolves to nothing at all: the candidate is excluded
// rather than downgraded, in both directions and under allow_inferred.
func TestRelatedNeverResolvesAcrossLanguages(t *testing.T) {
	records := []model.Record{
		pythonImport("a-import-load", "app/a.py", "load", "b", 1),
		pythonRecord("a-run", "app/a.py", "a.run", model.Definition, 3, 6),
		pythonReference("a-run-uses", "app/a.py", "a.run", 3, 6, []model.ReferenceEntry{{Name: "load", Line: 4, Count: 1}}),
		rustRecord("rs-b-load", "rs/b.rs", "b.load", model.Definition, 2, 5),
		// A Go package sharing one directory and one module name with a Python
		// module must not join that module's scope either.
		pythonReference("util-uses", "pkg/util.py", "util", 1, 6, []model.ReferenceEntry{{Name: "helper", Line: 3, Count: 1}}),
		goRecord("go-util-module", "pkg/util.go", "util", model.Module, 1, 1),
		goRecord("go-util-helper", "pkg/util.go", "util.helper", model.Definition, 3, 5),
	}
	snapshot := relatedSnapshot(records)
	for _, probe := range []struct{ direction, anchor string }{
		{"callees", "a-run"},
		{"callers", "rs-b-load"},
		{"callers", "go-util-helper"},
	} {
		request := relatedRequest(probe.direction, probe.anchor)
		request.AllowInferred = true
		response := Related(snapshot, request, policy.ProductionLimits())
		if len(response.Findings) != 0 {
			t.Fatalf("%s(%s) crossed languages: %#v", probe.direction, probe.anchor, response.Findings)
		}
	}
}

// Rule 1 sees only the names visible where the call is written. A method is not
// bare-callable from the file's module level, so an explicit import of the same
// name is the one candidate and the edge is verified.
func TestRelatedIgnoresAMethodThatIsNotVisibleAtTheCall(t *testing.T) {
	records := []model.Record{
		pythonImport("a-import-load", "pkg/a.py", "load", "pkg.b", 1),
		pythonRecord("a-class", "pkg/a.py", "a.A", model.Definition, 3, 6),
		pythonRecord("a-class-load", "pkg/a.py", "a.A.load", model.Definition, 4, 6),
		pythonRecord("a-run", "pkg/a.py", "a.run", model.Definition, 8, 10),
		pythonReference("a-run-uses", "pkg/a.py", "a.run", 8, 10, []model.ReferenceEntry{{Name: "load", Line: 9, Count: 1}}),
		pythonRecord("b-module", "pkg/b.py", "b", model.Module, 1, 4),
		pythonRecord("b-load", "pkg/b.py", "b.load", model.Definition, 2, 4),
	}
	snapshot := relatedSnapshot(records)

	callees := Related(snapshot, relatedRequest("callees", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(callees.Findings), []string{"b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callees of a.run = %#v, want %#v", got, want)
	}
	if callees.Findings[0].EdgeEvidence != model.Verified {
		t.Fatalf("callee edge = %#v", callees.Findings[0])
	}
	callers := Related(snapshot, relatedRequest("callers", "b-load"), policy.ProductionLimits())
	if got, want := relatedIdentities(callers.Findings), []string{"a-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers of b.load = %#v, want %#v", got, want)
	}
	if callers.Findings[0].EdgeEvidence != model.Verified {
		t.Fatalf("caller edge = %#v", callers.Findings[0])
	}
}

// Visibility narrows a bare name. A dotted target names its own scope, and the
// qualified name already had to end with it, so a receiver call still reaches
// the method it names.
func TestRelatedResolvesADottedReceiverCallToItsMethod(t *testing.T) {
	records := []model.Record{
		goRecord("engine-module", "internal/engine/engine.go", "engine", model.Module, 1, 1),
		goRecord("engine-type", "internal/engine/engine.go", "engine.Engine", model.Definition, 3, 20),
		goRecord("engine-execute", "internal/engine/engine.go", "engine.Engine.Execute", model.Definition, 10, 18),
		goRecord("engine-test-module", "internal/engine/engine_test.go", "engine", model.Module, 1, 1),
		goRecord("engine-test", "internal/engine/engine_test.go", "engine.TestExecute", model.Definition, 5, 12),
		goReference("engine-test-uses", "internal/engine/engine_test.go", "engine.TestExecute", 5, 12,
			[]model.ReferenceEntry{{Name: "engine.Execute", Line: 7, Count: 1}}),
	}
	snapshot := relatedSnapshot(records)
	response := Related(snapshot, relatedRequest("callers", "engine-execute"), policy.ProductionLimits())
	if got, want := relatedIdentities(response.Findings), []string{"engine-test"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callers of the method = %#v, want %#v", got, want)
	}
	if response.Findings[0].EdgeEvidence != model.Verified {
		t.Fatalf("receiver call edge = %#v", response.Findings[0])
	}
}

// When a definition visible at the call and an import bind the same name,
// neither is provably the target, so the edge is ambiguous and both candidates
// are inferred.
func TestRelatedTreatsAVisibleDefinitionAndAnImportOfOneNameAsAmbiguous(t *testing.T) {
	records := []model.Record{
		pythonImport("a-import-load", "pkg/a.py", "load", "pkg.b", 1),
		pythonRecord("a-load", "pkg/a.py", "a.load", model.Definition, 3, 5),
		pythonRecord("a-run", "pkg/a.py", "a.run", model.Definition, 8, 10),
		pythonReference("a-run-uses", "pkg/a.py", "a.run", 8, 10, []model.ReferenceEntry{{Name: "load", Line: 9, Count: 1}}),
		pythonRecord("b-module", "pkg/b.py", "b", model.Module, 1, 4),
		pythonRecord("b-load", "pkg/b.py", "b.load", model.Definition, 2, 4),
	}
	snapshot := relatedSnapshot(records)

	if hidden := Related(snapshot, relatedRequest("callers", "b-load"), policy.ProductionLimits()); len(hidden.Findings) != 0 {
		t.Fatalf("ambiguous callers of b.load without allow_inferred = %#v", hidden.Findings)
	}
	request := relatedRequest("callees", "a-run")
	request.AllowInferred = true
	callees := Related(snapshot, request, policy.ProductionLimits())
	if got, want := relatedIdentities(callees.Findings), []string{"a-load", "b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("ambiguous callees of a.run = %#v, want %#v", got, want)
	}
	for _, finding := range callees.Findings {
		if finding.EdgeEvidence != model.Inferred {
			t.Fatalf("ambiguous edge = %#v", finding)
		}
	}
}

// A candidate scan that could not finish has proved nothing about the
// candidates it never reached, so it must not report the one it did reach as
// the unambiguous target. Two definitions carry the imported name; the index
// still points at the second while the record slice no longer carries it, which
// is what a scan cut short looks like from inside the resolver.
func TestRelatedDoesNotClaimVerifiedFromATruncatedCandidateScan(t *testing.T) {
	records := []model.Record{
		goRecord("app-module", "app/main.go", "app", model.Module, 1, 1),
		goImport("app-import-store", "app/main.go", "store", "example.com/x/store", 3),
		goRecord("app-run", "app/main.go", "app.Run", model.Definition, 5, 8),
		goReference("app-run-uses", "app/main.go", "app.Run", 5, 8, []model.ReferenceEntry{{Name: "store.Load", Line: 6, Count: 1}}),
		goRecord("store-one-load", "store/one.go", "store.Load", model.Definition, 3, 6),
		goRecord("store-one-module", "store/one.go", "store", model.Module, 1, 1),
		goRecord("store-two-load", "store/two.go", "store.Load", model.Definition, 3, 6),
		goRecord("store-two-module", "store/two.go", "store", model.Module, 1, 1),
	}
	request := relatedRequest("callers", "store-one-load")
	request.AllowInferred = true

	// Both candidates present: the name is plainly ambiguous.
	whole := Related(relatedSnapshot(records), request, policy.ProductionLimits())
	if got, want := relatedIdentities(whole.Findings), []string{"app-run"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("ambiguous callers = %#v, want %#v", got, want)
	}
	if whole.Findings[0].EdgeEvidence != model.Inferred {
		t.Fatalf("ambiguous edge = %#v", whole.Findings[0])
	}

	truncated := Related(relatedTruncatedSnapshot(records, 2), request, policy.ProductionLimits())
	if !truncated.Partial {
		t.Fatalf("a scan that ran off the record slice is not partial: %#v", truncated)
	}
	for _, finding := range truncated.Findings {
		if finding.EdgeEvidence == model.Verified {
			t.Fatalf("a truncated scan manufactured a verified edge: %#v", finding)
		}
	}

	// The same must hold whatever the work budget stops the scan.
	for budget := 1; budget <= 600; budget++ {
		response := Related(relatedSnapshot(records), request, relatedLimits(budget))
		for _, finding := range response.Findings {
			if finding.EdgeEvidence == model.Verified {
				t.Fatalf("budget %d manufactured a verified edge: %#v", budget, finding)
			}
		}
	}
}

// A relative specifier names the neighbouring module, and imports() must read
// it the way callers and callees do, so one edge carries one evidence class
// whichever direction it is asked from.
func TestRelatedImportsResolveARelativeSpecifierInsideItsOwnDirectory(t *testing.T) {
	records := []model.Record{
		pythonImport("a-import-load", "pkg/a.py", "load", ".b", 1),
		pythonRecord("a-run", "pkg/a.py", "a.run", model.Definition, 3, 6),
		pythonReference("a-run-uses", "pkg/a.py", "a.run", 3, 6, []model.ReferenceEntry{{Name: "load", Line: 4, Count: 1}}),
		pythonRecord("pkg-b-load", "pkg/b.py", "b.load", model.Definition, 2, 4),
		pythonRecord("other-b-load", "other/b.py", "b.load", model.Definition, 2, 4),
	}
	snapshot := relatedSnapshot(records)

	imports := Related(snapshot, relatedRequest("imports", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(imports.Findings), []string{"pkg-b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("imports of a.run = %#v, want %#v", got, want)
	}
	if finding := imports.Findings[0]; finding.EdgeEvidence != model.Verified || finding.ReferenceLine != 1 {
		t.Fatalf("relative import edge = %#v", finding)
	}
	callees := Related(snapshot, relatedRequest("callees", "a-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(callees.Findings), []string{"pkg-b-load"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("callees of a.run = %#v, want %#v", got, want)
	}
	if callees.Findings[0].EdgeEvidence != model.Verified {
		t.Fatalf("relative call edge = %#v", callees.Findings[0])
	}
}

// relatedLimits is the production policy with one lowered record budget, so a
// test can put the resolver under budget pressure without touching the frozen
// policy artifact.
func relatedLimits(records int) policy.Limits {
	limits := policy.ProductionLimits()
	limits.MaximumLexicalCandidates = records
	return limits
}

// relatedTruncatedSnapshot keeps the query index of the whole record set but
// hands the resolver a shorter record slice, so a posting entry runs off the
// end exactly as it does when a scan cannot finish.
func relatedTruncatedSnapshot(records []model.Record, dropped int) store.Snapshot {
	sorted := append([]model.Record(nil), records...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Identity < sorted[j].Identity })
	return store.Snapshot{Records: sorted[:len(sorted)-dropped], Query: store.BuildQueryIndex(sorted)}
}

func rustRecord(identity, path, name string, kind model.RecordKind, start, end int) model.Record {
	record := pythonRecord(identity, path, name, kind, start, end)
	record.Language, record.ExtractionMethod = "rust", "tree-sitter-rust"
	return record
}

// A Go package's files may sort a test file first, and naming a test file as
// the package an import names is a poor answer: the representative is the
// first non-test file of the directory, and a package that is only tests still
// resolves.
func TestRelatedPrefersANonTestFileAsTheGoPackageRepresentative(t *testing.T) {
	records := []model.Record{
		goRecord("boundary-test-module", "internal/boundary/boundary_test.go", "boundary", model.Module, 1, 1),
		goRecord("boundary-module", "internal/boundary/roots.go", "boundary", model.Module, 1, 1),
		goRecord("boundary-roots", "internal/boundary/roots.go", "boundary.Roots", model.Definition, 3, 6),
		goRecord("app-module", "app/main.go", "app", model.Module, 1, 1),
		goImport("app-import-boundary", "app/main.go", "boundary", "example.com/x/internal/boundary", 3),
		goRecord("app-run", "app/main.go", "app.Run", model.Definition, 5, 8),
		goReference("app-run-uses", "app/main.go", "app.Run", 5, 8, []model.ReferenceEntry{{Name: "boundary.Roots", Line: 6, Count: 1}}),
	}
	imports := Related(relatedSnapshot(records), relatedRequest("imports", "app-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(imports.Findings), []string{"boundary-module"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("imports = %#v, want the package's first non-test file %#v", got, want)
	}

	onlyTests := []model.Record{
		goRecord("probe-test-module", "internal/probe/probe_test.go", "probe", model.Module, 1, 1),
		goRecord("app-module", "app/main.go", "app", model.Module, 1, 1),
		goImport("app-import-probe", "app/main.go", "probe", "example.com/x/internal/probe", 3),
		goRecord("app-run", "app/main.go", "app.Run", model.Definition, 5, 8),
		goReference("app-run-uses", "app/main.go", "app.Run", 5, 8, []model.ReferenceEntry{{Name: "probe.Check", Line: 6, Count: 1}}),
	}
	testsOnly := Related(relatedSnapshot(onlyTests), relatedRequest("imports", "app-run"), policy.ProductionLimits())
	if got, want := relatedIdentities(testsOnly.Findings), []string{"probe-test-module"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("imports of a test-only package = %#v, want %#v", got, want)
	}
}
