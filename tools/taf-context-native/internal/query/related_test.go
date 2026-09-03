package query

import (
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
