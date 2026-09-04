package engine

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestBuiltGenerationServesAllReadOnlyQueryOperations(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	for _, operation := range []wire.Operation{wire.RepositoryMap, wire.SearchSymbols, wire.SearchDocs} {
		envelope := controlEnvelope(operation, repository, state, built.IndexIdentity)
		if operation != wire.RepositoryMap {
			query := "main"
			envelope.Request.Query = &query
		}
		result, executeErr := engine.Execute(context.Background(), envelope)
		if executeErr != nil {
			t.Fatalf("%s: %v", operation, executeErr)
		}
		if result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" {
			t.Fatalf("%s result = %#v", operation, result)
		}
		if operation == wire.SearchSymbols && len(result.Findings) == 0 {
			t.Fatalf("%s returned no indexed evidence", operation)
		}
	}
}

func TestCachedEngineValidatesAndLoadsAnUnchangedGenerationOnce(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	inspect, load := dependencies.Peek, dependencies.Load
	inspectCalls, loadCalls := 0, 0
	dependencies.Peek = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
		inspectCalls++
		return inspect(ctx, roots)
	}
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		loadCalls++
		return load(ctx, roots, identity)
	}
	cached := NewCached(dependencies)
	for index := 0; index < 2; index++ {
		result, executeErr := cached.Execute(context.Background(), controlEnvelope(wire.RepositoryMap, repository, state, built.IndexIdentity))
		if executeErr != nil || result.Freshness != "exact" || len(result.Findings) == 0 {
			t.Fatalf("query %d = %#v, %v", index, result, executeErr)
		}
	}
	if inspectCalls != 1 || loadCalls != 1 {
		t.Fatalf("inspect=%d load=%d, want one validated materialization", inspectCalls, loadCalls)
	}
}

func TestQueryRefusesStaleBindingWithoutFindings(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	query := "main"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &query
	envelope.Request.WorktreeIdentity = "sha256:bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a"
	result, executeErr := engine.Execute(context.Background(), envelope)
	if executeErr != nil {
		t.Fatal(executeErr)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
		t.Fatalf("stale query = %#v", result)
	}
}

func TestQueryWrongIndexIsStructurallyStaleWithoutFindings(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	if _, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil)); err != nil {
		t.Fatal(err)
	}
	query := "main"
	wrong := "sha256:bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, &wrong)
	envelope.Request.Query = &query
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || len(result.Findings) != 0 {
		t.Fatalf("wrong index = %#v", result)
	}
}

func TestQueryTreatsInspectLoadIdentityRaceAsStaleWithoutFindings(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	dependencies.Load = func(context.Context, *boundary.Roots, string) (store.Snapshot, error) {
		return store.Snapshot{}, store.ErrIndexMismatch
	}
	queryEngine := New(dependencies)
	queryText := "main"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText
	result, err := queryEngine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
		t.Fatalf("Inspect/Load race = %#v", result)
	}
}

func TestQueryRechecksLoadedManifestFreshnessWithoutFindings(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		snapshot.Manifest.Binding.CommittedHead = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
		return snapshot, loadErr
	}
	queryEngine := New(dependencies)
	queryText := "main"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText
	result, err := queryEngine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "incrementally-stale" || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
		t.Fatalf("loaded freshness = %#v", result)
	}
}

func TestQueryPlannerExhaustionIsPartialExactAndRetainsFindings(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		if loadErr != nil {
			return store.Snapshot{}, loadErr
		}
		records := make([]model.Record, 4097)
		for index := range records {
			terms := make([]string, 0, 8)
			for term := 0; term < 8; term++ {
				terms = append(terms, fmt.Sprintf("prefix%d_%d", index, term))
			}
			records[index] = model.Record{
				Identity: fmt.Sprintf("sha256:%064x", index+1), Path: fmt.Sprintf("pkg/%05d.go", index),
				StartLine: 1, EndLine: 1, Language: "go", RecordKind: model.Definition,
				SourceType: "source", QualifiedName: fmt.Sprintf("prefix%d", index),
				ExtractionMethod: "test", EvidenceClass: model.Verified,
				SearchTerms: terms,
			}
		}
		snapshot.Records = records
		snapshot.Query = store.BuildQueryIndex(records)
		return snapshot, nil
	}
	queryEngine := New(dependencies)
	queryText := "prefix"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText
	result, err := queryEngine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Partial || result.Freshness != "exact" || result.NextSafeAction != "refine-query" || len(result.Findings) == 0 || !result.Truncated || !hasWarning(result.Warnings, "query-frontier-exhausted") {
		t.Fatalf("planner exhaustion = %#v", result)
	}
}

func TestRankingOverflowIsReadyAndTruncatedWithCountedOmissions(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		if loadErr != nil {
			return store.Snapshot{}, loadErr
		}
		records := make([]model.Record, 100)
		for index := range records {
			records[index] = model.Record{
				Identity: fmt.Sprintf("sha256:%064x", index+1), Path: fmt.Sprintf("pkg/%05d.go", index),
				StartLine: 1, EndLine: 1, Language: "go", RecordKind: model.Definition,
				SourceType: "source", QualifiedName: fmt.Sprintf("pkg.Service%d", index),
				ExtractionMethod: "test", EvidenceClass: model.Verified, SearchTerms: []string{"service"},
			}
		}
		snapshot.Records = records
		snapshot.Query = store.BuildQueryIndex(records)
		return snapshot, nil
	}
	queryText := "service"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText
	envelope.Request.MaximumResults = 8
	result, err := New(dependencies).Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.NextSafeAction != "use-index" || !result.Truncated || result.OmittedCount != 92 || len(result.Findings) != 8 || hasWarning(result.Warnings, "query-frontier-exhausted") {
		t.Fatalf("ranking overflow = %#v", result)
	}
}

// TestDictionaryExhaustionIsTruncatedWithoutCountedOmissions needs a dictionary
// larger than the per-query term budget (262,144) so the substring scan windows
// itself and reports Partial with zero matches; the large fixture is what makes
// Truncated reachable only through response.Partial, never through Omitted.
func TestDictionaryExhaustionIsTruncatedWithoutCountedOmissions(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		if loadErr != nil {
			return store.Snapshot{}, loadErr
		}
		const recordCount = 4100
		records := make([]model.Record, recordCount)
		for index := range records {
			terms := make([]string, 0, 64)
			for term := 0; term < 64; term++ {
				terms = append(terms, fmt.Sprintf("t%dx%d", index, term))
			}
			records[index] = model.Record{
				Identity: fmt.Sprintf("sha256:%064x", index+1), Path: fmt.Sprintf("pkg/%05d.go", index),
				StartLine: 1, EndLine: 1, Language: "go", RecordKind: model.Definition,
				SourceType: "source", QualifiedName: fmt.Sprintf("pkg.R%d", index),
				ExtractionMethod: "test", EvidenceClass: model.Verified,
				SearchTerms: terms,
			}
		}
		snapshot.Records = records
		snapshot.Query = store.BuildQueryIndex(records)
		return snapshot, nil
	}
	queryEngine := New(dependencies)
	queryText := "zzzz"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText
	envelope.Request.MaximumResults = 8
	result, err := queryEngine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Partial || result.Freshness != "exact" || result.NextSafeAction != "refine-query" || !result.Truncated || result.OmittedCount != 0 || len(result.Findings) != 0 || !hasWarning(result.Warnings, "query-frontier-exhausted") {
		t.Fatalf("dictionary exhaustion = %#v", result)
	}
}

func TestQueryLoadsUsablePartialStateButRejectsCorruptState(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	queryText := "main"
	envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	envelope.Request.Query = &queryText

	t.Run("partial", func(t *testing.T) {
		dependencies := ProductionDependencies()
		inspect := dependencies.Peek
		load := dependencies.Load
		loaded := false
		dependencies.Peek = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
			status, inspectErr := inspect(ctx, roots)
			status.Manifest.Coverage.ParseFailureCount = 1
			return status, inspectErr
		}
		dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
			loaded = true
			snapshot, loadErr := load(ctx, roots, identity)
			snapshot.Manifest.Coverage.ParseFailureCount = 1
			return snapshot, loadErr
		}
		result, executeErr := New(dependencies).Execute(context.Background(), envelope)
		if executeErr != nil {
			t.Fatal(executeErr)
		}
		if result.Status != wire.Partial || result.Freshness != "exact" || !loaded || len(result.Findings) == 0 || result.NextSafeAction != "use-index" {
			t.Fatalf("partial state = %#v loaded=%v", result, loaded)
		}
	})

	t.Run("corrupt", func(t *testing.T) {
		dependencies := ProductionDependencies()
		dependencies.Peek = func(context.Context, *boundary.Roots) (store.Status, error) {
			return store.Status{}, store.ErrStoreCorrupt
		}
		result, executeErr := New(dependencies).Execute(context.Background(), envelope)
		if executeErr != nil {
			t.Fatal(executeErr)
		}
		if result.Status != wire.Error || result.Freshness != "unusable" || len(result.Findings) != 0 {
			t.Fatalf("corrupt state = %#v", result)
		}
	})
}

func TestCachedEngineServesTwoStateRootsAndTheUpdatedGeneration(t *testing.T) {
	repositoryA, stateA := controlRoots(t)
	repositoryB, stateB := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repositoryB, "other.go"), []byte("package sample\nfunc Other() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cached := NewCached(ProductionDependencies())
	builtA := mustBuildForUpdate(t, cached, repositoryA, stateA)
	builtB := mustBuildForUpdate(t, cached, repositoryB, stateB)
	if *builtA.IndexIdentity == *builtB.IndexIdentity {
		t.Fatal("the two repositories must produce distinct index identities")
	}
	search := func(envelope wire.Envelope, term string) wire.Result {
		t.Helper()
		envelope.Request.Query = &term
		result, err := cached.Execute(context.Background(), envelope)
		if err != nil {
			t.Fatalf("%s: %v", term, err)
		}
		return result
	}
	// A, then B, then A again: each answer comes from its own generation.
	for _, step := range []struct {
		repository, state, want string
		index                   *string
	}{
		{repositoryA, stateA, "Main", builtA.IndexIdentity},
		{repositoryB, stateB, "Other", builtB.IndexIdentity},
		{repositoryA, stateA, "Main", builtA.IndexIdentity},
	} {
		result := search(controlEnvelope(wire.SearchSymbols, step.repository, step.state, step.index), step.want)
		if result.Freshness != "exact" || len(result.Findings) == 0 || !strings.HasSuffix(result.Findings[0].QualifiedName, step.want) {
			t.Fatalf("%s: result = %#v", step.want, result)
		}
	}
	// Update A; the cache must serve the new generation and refuse the superseded identity.
	if err := os.WriteFile(filepath.Join(repositoryA, "main.go"), []byte("package sample\nfunc Renamed() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, stateA, updateDocument(t, builtA.IndexIdentity, "main.go"))
	updated, err := cached.Execute(context.Background(), validUpdateEnvelope(repositoryA, stateA, builtA.IndexIdentity))
	if err != nil || updated.Status != wire.Ready || updated.IndexIdentity == nil {
		t.Fatalf("update = %#v, %v", updated, err)
	}
	after := validUpdateEnvelope(repositoryA, stateA, updated.IndexIdentity).Request
	fresh := controlEnvelope(wire.SearchSymbols, repositoryA, stateA, updated.IndexIdentity)
	fresh.Request.RepositoryIdentity, fresh.Request.WorktreeIdentity = after.RepositoryIdentity, after.WorktreeIdentity
	fresh.Request.CommittedHead, fresh.Request.DirtyOverlayFingerprint = after.CommittedHead, after.DirtyOverlayFingerprint
	if result := search(fresh, "Renamed"); result.Freshness != "exact" || len(result.Findings) == 0 {
		t.Fatalf("updated generation not served: %#v", result)
	}
	stale := fresh
	stale.Request.IndexIdentity = builtA.IndexIdentity
	if result := search(stale, "Main"); result.Status != wire.Stale || len(result.Findings) != 0 {
		t.Fatalf("superseded identity must be refused as stale without findings: %#v", result)
	}
}

// relatedRoots is one Go package spread over two files, so a call in one file
// is answered by the definition the other file carries.
func relatedRoots(t *testing.T) (string, string) {
	t.Helper()
	base := t.TempDir()
	repository := filepath.Join(base, "repository")
	if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"caller.go": "package sample\n\nfunc Main() {\n\thelper()\n\thelper()\n}\n",
		"helper.go": "package sample\n\nfunc helper() {}\n",
	}
	for name, contents := range files {
		if err := os.WriteFile(filepath.Join(repository, name), []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return repository, filepath.Join(base, "state")
}

func relatedEnvelope(repository, state string, index *string, direction string, identities []string) wire.Envelope {
	envelope := controlEnvelope(wire.RelatedSymbols, repository, state, index)
	envelope.Request.SchemaVersion = "2"
	envelope.Request.Direction = &direction
	envelope.Request.ResultIdentities = identities
	return envelope
}

func queryFinding(t *testing.T, result wire.Result, qualified string) wire.Finding {
	t.Helper()
	for _, finding := range result.Findings {
		if finding.QualifiedName == qualified {
			return finding
		}
	}
	t.Fatalf("no finding named %q in %#v", qualified, result.Findings)
	return wire.Finding{}
}

func TestRelatedSymbolsAnswersCallersWithPerEdgeEvidence(t *testing.T) {
	repository, state := relatedRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	search := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	text := "helper"
	search.Request.Query = &text
	symbols, err := engine.Execute(context.Background(), search)
	if err != nil {
		t.Fatal(err)
	}
	anchor := queryFinding(t, symbols, "sample.helper")

	result, err := engine.Execute(context.Background(), relatedEnvelope(repository, state, built.IndexIdentity, "callers", []string{anchor.ResultIdentity}))
	if err != nil {
		t.Fatal(err)
	}
	if result.SchemaVersion != "2" || result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" {
		t.Fatalf("related result = %#v", result)
	}
	caller := queryFinding(t, result, "sample.Main")
	if caller.Relation != "call" || caller.EdgeEvidence != "verified" || caller.ReferenceLine != 4 || caller.ReferenceCount != 2 {
		t.Fatalf("caller finding = %#v", caller)
	}
	if caller.RecordKind != "definition" || caller.Path != "caller.go" {
		t.Fatalf("caller record = %#v", caller)
	}

	callees, err := engine.Execute(context.Background(), relatedEnvelope(repository, state, built.IndexIdentity, "callees", []string{caller.ResultIdentity}))
	if err != nil {
		t.Fatal(err)
	}
	if callee := queryFinding(t, callees, "sample.helper"); callee.Relation != "call" || callee.EdgeEvidence != "verified" || callee.ResultIdentity != anchor.ResultIdentity {
		t.Fatalf("callee finding = %#v", callee)
	}
}

// TestRelatedSymbolsRefusesAnAnchorThatIsNotASymbol covers the three anchors a
// relationship cannot start from: an identity the index does not carry, a
// reference record - a use of a name - and an import binding. The last two are
// real records of the built index, so they pass every wire check and are
// refused for what they are.
func TestRelatedSymbolsRefusesAnAnchorThatIsNotASymbol(t *testing.T) {
	repository, state := relatedRootsWithImport(t)
	dependencies := ProductionDependencies()
	load := dependencies.Load
	var indexed store.Snapshot
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, err := load(ctx, roots, identity)
		if err == nil {
			indexed = snapshot
		}
		return snapshot, err
	}
	engine := New(dependencies)
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	search := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	text := "helper"
	search.Request.Query = &text
	if _, searchErr := engine.Execute(context.Background(), search); searchErr != nil {
		t.Fatal(searchErr)
	}
	anchors := map[string]string{"unknown": "sha256:" + strings.Repeat("b", 64)}
	for _, kind := range []model.RecordKind{model.Reference, model.Import} {
		for _, record := range indexed.Records {
			if record.RecordKind == kind {
				anchors[string(kind)] = record.Identity
				break
			}
		}
	}
	if len(anchors) != 3 {
		t.Fatalf("anchors = %#v, want an unknown, a reference and an import identity", anchors)
	}
	for name, anchor := range anchors {
		result, executeErr := engine.Execute(context.Background(), relatedEnvelope(repository, state, built.IndexIdentity, "callers", []string{anchor}))
		if executeErr != nil {
			t.Fatal(executeErr)
		}
		if result.SchemaVersion != "2" || result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 {
			t.Fatalf("%s anchor = %#v", name, result)
		}
	}
}

// relatedRootsWithImport carries an import binding and a call as well, so a
// test can anchor on the two record kinds no query operation returns.
func relatedRootsWithImport(t *testing.T) (string, string) {
	t.Helper()
	base := t.TempDir()
	repository := filepath.Join(base, "repository")
	if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"caller.go": "package sample\n\nimport \"strings\"\n\nfunc Main() {\n\thelper()\n\tstrings.ToUpper(\"x\")\n}\n",
		"helper.go": "package sample\n\nfunc helper() {}\n",
	}
	for name, contents := range files {
		if err := os.WriteFile(filepath.Join(repository, name), []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return repository, filepath.Join(base, "state")
}

// A result always answers in the schema its request asked for, and the edge
// fields stay empty for every operation that resolves no edge.
func TestQueryResultsEchoTheRequestSchemaVersion(t *testing.T) {
	repository, state := relatedRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	for _, schema := range []string{"1", "2"} {
		envelope := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
		envelope.Request.SchemaVersion = schema
		text := "helper"
		envelope.Request.Query = &text
		result, executeErr := engine.Execute(context.Background(), envelope)
		if executeErr != nil {
			t.Fatalf("schema %s: %v", schema, executeErr)
		}
		if result.SchemaVersion != schema || len(result.Findings) == 0 {
			t.Fatalf("schema %s result = %#v", schema, result)
		}
		for _, finding := range result.Findings {
			if finding.Relation != "" || finding.EdgeEvidence != "" || finding.ReferenceLine != 0 || finding.ReferenceCount != 0 {
				t.Fatalf("schema %s search finding carries edge fields: %#v", schema, finding)
			}
		}
	}
}

func changedEnvelope(repository, state string, index *string, entries ...wire.ChangedRange) wire.Envelope {
	envelope := controlEnvelope(wire.ChangedSymbols, repository, state, index)
	envelope.Request.SchemaVersion = "3"
	selector := append([]wire.ChangedRange(nil), entries...)
	envelope.Request.ChangedRanges = &selector
	return envelope
}

// The change set is the same evidence source as every other query operation:
// the built generation. A hunk inside one function therefore answers with that
// function alone, in the schema the request asked for and at the engine
// version the release pins.
func TestChangedSymbolsAnswersTheSymbolsAHunkTouches(t *testing.T) {
	repository, state := relatedRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	// caller.go line 4 is the first call inside sample.Main, which spans lines
	// 3 to 6; the package clause record of the file covers line 1 only.
	result, err := engine.Execute(context.Background(), changedEnvelope(repository, state, built.IndexIdentity, wire.ChangedRange{Path: "caller.go", Ranges: [][2]int{{4, 4}}}))
	if err != nil {
		t.Fatal(err)
	}
	if result.SchemaVersion != "3" || result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" {
		t.Fatalf("changed result = %#v", result)
	}
	if result.ProviderVersion != "0.5.0" {
		t.Fatalf("provider version = %q, want 0.5.0", result.ProviderVersion)
	}
	if len(result.Findings) != 1 {
		t.Fatalf("findings = %#v, want sample.Main alone", result.Findings)
	}
	finding := result.Findings[0]
	if finding.QualifiedName != "sample.Main" || finding.Path != "caller.go" || finding.RecordKind != "definition" {
		t.Fatalf("finding = %#v", finding)
	}
	if finding.Relation != "" || finding.EdgeEvidence != "" || finding.ReferenceLine != 0 || finding.ReferenceCount != 0 {
		t.Fatalf("schema-3 changed finding carries edge fields: %#v", finding)
	}
	if result.OmittedCount != 0 || result.Truncated || len(result.Warnings) != 0 {
		t.Fatalf("changed result accounting = %#v", result)
	}

	// A whole-file entry admits every symbol of the path, including the module
	// record the Go package clause carries.
	whole, err := engine.Execute(context.Background(), changedEnvelope(repository, state, built.IndexIdentity, wire.ChangedRange{Path: "helper.go", Ranges: [][2]int{}}))
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(whole.Findings))
	for _, item := range whole.Findings {
		names = append(names, item.QualifiedName)
	}
	if !reflect.DeepEqual(names, []string{"sample", "sample.helper"}) {
		t.Fatalf("whole-file findings = %#v, want the module record and the definition", names)
	}
}

// A changed path the index does not carry is not an omission and not an error:
// the engine reports it once, so the caller knows the change set reached
// further than the index.
func TestChangedSymbolsWarnsOnceForPathsTheIndexDoesNotCarry(t *testing.T) {
	repository, state := relatedRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	result, err := engine.Execute(context.Background(), changedEnvelope(repository, state, built.IndexIdentity,
		wire.ChangedRange{Path: "caller.go", Ranges: [][2]int{{4, 4}}},
		wire.ChangedRange{Path: "docs/notes.md", Ranges: [][2]int{}},
		wire.ChangedRange{Path: "vendor/gone.go", Ranges: [][2]int{}},
	))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.OmittedCount != 0 || result.Truncated {
		t.Fatalf("changed result = %#v", result)
	}
	if !reflect.DeepEqual(result.Warnings, []string{"changed-path-not-indexed"}) {
		t.Fatalf("warnings = %#v, want exactly one changed-path-not-indexed", result.Warnings)
	}
	if len(result.Findings) != 1 || result.Findings[0].QualifiedName != "sample.Main" {
		t.Fatalf("findings = %#v", result.Findings)
	}
}

// overviewRoots builds a control repository with the four shapes an overview
// has to tell apart: a command, a package, a document, and configuration.
func overviewRoots(t *testing.T) (string, string) {
	t.Helper()
	base := t.TempDir()
	repository := filepath.Join(base, "repository")
	for _, directory := range []string{"", ".git", "cmd", filepath.Join("cmd", "tool"), "internal", filepath.Join("internal", "a"), "docs"} {
		if err := os.Mkdir(filepath.Join(repository, directory), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for relative, body := range map[string]string{
		filepath.Join("cmd", "tool", "main.go"): "package main\n\nfunc helper() {}\n\nfunc main() {\n\thelper()\n}\n",
		filepath.Join("internal", "a", "x.go"):  "package a\n\nfunc X() {}\n",
		filepath.Join("docs", "README.md"):      "# Control\n\nHow this control repository is organized.\n",
		"config.json":                           "{\n  \"name\": \"control\"\n}\n",
	} {
		if err := os.WriteFile(filepath.Join(repository, relative), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return repository, filepath.Join(base, "state")
}

// The overview answers how a repository is organized in one call: the group
// table names the directories, and the file layer starts at the command's
// entry point rather than at the first path in alphabetical order.
func TestRepositoryOverviewDescribesTheControlRepository(t *testing.T) {
	repository, state := overviewRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	envelope := controlEnvelope(wire.RepositoryOverview, repository, state, built.IndexIdentity)
	envelope.Request.SchemaVersion = "4"
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" {
		t.Fatalf("overview result = %#v", result)
	}
	if result.SchemaVersion != "4" || result.ProviderVersion != "0.5.0" {
		t.Fatalf("schema = %q provider = %q, want \"4\" and \"0.5.0\"", result.SchemaVersion, result.ProviderVersion)
	}
	if result.Groups == nil || result.Overview == nil {
		t.Fatal("a schema-4 result carries both overview keys")
	}
	prefixes := make([]string, 0, len(*result.Groups))
	for _, group := range *result.Groups {
		prefixes = append(prefixes, group.PathPrefix)
	}
	if want := []string{"cmd/", "internal/", ".", "docs/"}; !reflect.DeepEqual(prefixes, want) {
		t.Fatalf("group prefixes = %#v, want %#v", prefixes, want)
	}
	command := (*result.Groups)[0]
	if command.Depth != 1 || command.FileCount != 1 || command.DefinitionCount != 1 || command.EntryPointCount != 1 || command.DocumentCount != 0 || command.ConfigurationCount != 0 {
		t.Fatalf("cmd/ group = %#v", command)
	}
	if got, want := command.Languages, []wire.OverviewLanguage{{Language: "go", FileCount: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("cmd/ languages = %#v, want %#v", got, want)
	}
	if command.RepresentativeIdentity == nil || *command.RepresentativeIdentity != result.Findings[0].ResultIdentity {
		t.Fatalf("cmd/ representative = %#v, first finding = %q", command.RepresentativeIdentity, result.Findings[0].ResultIdentity)
	}
	if document := (*result.Groups)[3]; document.DocumentCount != 1 || document.FileCount != 1 {
		t.Fatalf("docs/ group = %#v", document)
	}
	if root := (*result.Groups)[2]; root.Depth != 0 || root.ConfigurationCount != 1 {
		t.Fatalf("root group = %#v", root)
	}
	if want := (wire.OverviewSummary{Root: "", CountedFileCount: 4, OtherGroupCount: 0}); *result.Overview != want {
		t.Fatalf("summary = %#v, want %#v", *result.Overview, want)
	}
	paths := make([]string, 0, len(result.Findings))
	for _, finding := range result.Findings {
		paths = append(paths, finding.Path)
	}
	if want := []string{filepath.ToSlash("cmd/tool/main.go"), "internal/a/x.go", "config.json", "docs/README.md"}; !reflect.DeepEqual(paths, want) {
		t.Fatalf("file layer = %#v, want %#v", paths, want)
	}
	if result.ReturnedCount != 4 || result.OmittedCount != 0 || result.Truncated {
		t.Fatalf("counts = %d/%d truncated = %v", result.ReturnedCount, result.OmittedCount, result.Truncated)
	}
}

// A path prefix narrows the overview to a subtree and the summary names the
// normalized root, so a consumer can join a group prefix to it.
func TestRepositoryOverviewNarrowsToARequestedSubtree(t *testing.T) {
	repository, state := overviewRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	envelope := controlEnvelope(wire.RepositoryOverview, repository, state, built.IndexIdentity)
	envelope.Request.SchemaVersion = "4"
	envelope.Request.Filters.PathPrefixes = []string{"cmd"}
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Overview == nil || result.Overview.Root != "cmd/" || result.Overview.CountedFileCount != 1 {
		t.Fatalf("summary = %#v", result.Overview)
	}
	if len(*result.Groups) != 1 || (*result.Groups)[0].PathPrefix != "cmd/tool/" {
		t.Fatalf("groups = %#v", *result.Groups)
	}
}

// A path prefix that names the right directories through an irregular
// spelling — an interior "." segment, an empty segment from a doubled
// separator, or a trailing "." segment — normalizes to the same directory
// prefix a plain spelling would, and the request still encodes: the wire
// accepts all three as a path prefix, and the overview must answer them
// rather than fail its own result validation.
func TestRepositoryOverviewNormalizesAnIrregularPathPrefix(t *testing.T) {
	repository, state := overviewRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	for _, testCase := range []struct {
		name   string
		prefix string
		root   string
		groups []string
	}{
		{name: "interior dot segment", prefix: "cmd/./tool", root: "cmd/tool/", groups: []string{"cmd/tool/."}},
		{name: "doubled separator", prefix: "cmd//tool", root: "cmd/tool/", groups: []string{"cmd/tool/."}},
		{name: "trailing dot segment", prefix: "cmd/.", root: "cmd/", groups: []string{"cmd/tool/"}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			envelope := controlEnvelope(wire.RepositoryOverview, repository, state, built.IndexIdentity)
			envelope.Request.SchemaVersion = "4"
			envelope.Request.Filters.PathPrefixes = []string{testCase.prefix}
			result, err := engine.Execute(context.Background(), envelope)
			if err != nil {
				t.Fatalf("prefix %q must still encode: %v", testCase.prefix, err)
			}
			if result.Overview == nil || result.Overview.Root != testCase.root || result.Overview.CountedFileCount != 1 {
				t.Fatalf("summary = %#v, want root %q", result.Overview, testCase.root)
			}
			prefixes := make([]string, 0, len(*result.Groups))
			for _, group := range *result.Groups {
				prefixes = append(prefixes, group.PathPrefix)
			}
			if !reflect.DeepEqual(prefixes, testCase.groups) {
				t.Fatalf("groups = %#v, want %#v", prefixes, testCase.groups)
			}
		})
	}
}

// Several path prefixes root the overview at the first of them and say so, so
// the answer stays one honest subtree rather than a union of several.
func TestRepositoryOverviewWarnsWhenSeveralPrefixesAreRequested(t *testing.T) {
	repository, state := overviewRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	envelope := controlEnvelope(wire.RepositoryOverview, repository, state, built.IndexIdentity)
	envelope.Request.SchemaVersion = "4"
	envelope.Request.Filters.PathPrefixes = []string{"cmd", "internal"}
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Overview == nil || result.Overview.Root != "cmd/" {
		t.Fatalf("summary = %#v", result.Overview)
	}
	if !hasWarning(result.Warnings, "overview-root-first-prefix") {
		t.Fatalf("warnings = %#v", result.Warnings)
	}
}

// Every schema-4 result carries the two keys the schema promises, whatever
// status it reports and whichever operation asked: the result builder fills an
// empty table, so a refusal under schema 4 is still encodable.
func TestSchemaFourResultsAlwaysCarryTheOverviewKeys(t *testing.T) {
	engine := New(ProductionDependencies())
	request := controlEnvelope(wire.RepositoryOverview, "", "", testPtr(engineSHA)).Request
	request.SchemaVersion = "4"
	for _, testCase := range []struct {
		name   string
		result wire.Result
	}{
		{name: "unsupported", result: engine.unsupported(request)},
		{name: "error", result: engine.result(request, wire.Error, "unusable", nil, emptyCoverage(), "build-index")},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			result := testCase.result
			if result.Groups == nil || len(*result.Groups) != 0 || result.Overview == nil {
				t.Fatalf("result = %#v", result)
			}
			result.OutputCharacters = wire.OutputCharacters(result)
			if err := wire.EncodeResult(io.Discard, result); err != nil {
				t.Fatalf("schema-4 %s result does not encode: %v", testCase.name, err)
			}
		})
	}
	frozen := controlEnvelope(wire.RepositoryMap, "", "", testPtr(engineSHA)).Request
	if result := engine.unsupported(frozen); result.Groups != nil || result.Overview != nil {
		t.Fatalf("frozen schema result = %#v", result)
	}
}
