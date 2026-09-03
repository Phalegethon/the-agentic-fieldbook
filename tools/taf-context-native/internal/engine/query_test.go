package engine

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
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

func TestRelatedSymbolsRefusesAnAnchorThatIsNotASymbol(t *testing.T) {
	repository, state := relatedRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	unknown := "sha256:" + strings.Repeat("b", 64)
	result, err := engine.Execute(context.Background(), relatedEnvelope(repository, state, built.IndexIdentity, "callers", []string{unknown}))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 {
		t.Fatalf("unknown anchor = %#v", result)
	}
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
