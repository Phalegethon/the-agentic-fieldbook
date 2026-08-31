package engine

import (
	"context"
	"fmt"
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
	inspect, load := dependencies.Inspect, dependencies.Load
	inspectCalls, loadCalls := 0, 0
	dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
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
			records[index] = model.Record{
				Identity: fmt.Sprintf("sha256:%064x", index+1), Path: fmt.Sprintf("pkg/%05d.go", index),
				StartLine: 1, EndLine: 1, Language: "go", RecordKind: model.Definition,
				SourceType: "source", QualifiedName: fmt.Sprintf("prefix%d", index),
				ExtractionMethod: "test", EvidenceClass: model.Verified,
				SearchTerms: []string{fmt.Sprintf("prefix%d", index)},
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
	if result.Status != wire.Partial || result.Freshness != "exact" || result.NextSafeAction != "refine-query" || len(result.Findings) == 0 || !hasWarning(result.Warnings, "query-frontier-exhausted") {
		t.Fatalf("planner exhaustion = %#v", result)
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
		inspect := dependencies.Inspect
		load := dependencies.Load
		loaded := false
		dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
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
		dependencies.Inspect = func(context.Context, *boundary.Roots) (store.Status, error) {
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
