package engine

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/extract"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// These tests exercise the operation through the public engine dispatch rather
// than update helpers, so a fallback build or an early source read is visible.
func TestUpdateRejectsInvalidControlBeforeChangedSourceOpen(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)

	for name, document := range map[string]*string{
		"absent":   nil,
		"absolute": testPtr("/changes.json"),
		"outside":  testPtr("../changes.json"),
	} {
		t.Run(name, func(t *testing.T) {
			result, err := engine.Execute(context.Background(), updateEnvelope(repository, state, built.IndexIdentity, document))
			if err != nil {
				t.Fatal(err)
			}
			if result.Status != wire.Stale || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
				t.Fatalf("invalid update = %#v", result)
			}
		})
	}
}

func TestUpdateControlDocumentIsStateRootOwned(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity))
	if err := os.WriteFile(filepath.Join(repository, ".taf-update.txt"), []byte(`{"schema_version":"wrong"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.NextSafeAction != "use-index" {
		t.Fatalf("repository control shadow influenced update: %#v", result)
	}
}

func TestUpdateRejectsNullChangedPaths(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	writeUpdateDocument(t, state, updateDocumentWithPaths(t, built.IndexIdentity, nil))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("null changed_paths = %#v", result)
	}
}

func TestUpdateStrictlyRejectsMalformedDocumentAndBindings(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	valid := updateDocument(t, built.IndexIdentity, "main.go")
	opened := 0
	originalOpen := engine.dependencies.OpenFile
	engine.dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opened++
		return originalOpen(roots, relative, maximum)
	}
	request := validUpdateEnvelope(repository, state, built.IndexIdentity)

	cases := map[string][]byte{
		"duplicate":      append([]byte(`{"schema_version":"1",`), valid[1:]...),
		"unknown":        append(valid[:len(valid)-1], []byte(`,"unexpected":true}`)...),
		"missing":        []byte(`{"schema_version":"1"}`),
		"unsorted":       updateDocumentWithPaths(t, built.IndexIdentity, []string{"z.go", "a.go"}),
		"duplicate-path": updateDocumentWithPaths(t, built.IndexIdentity, []string{"main.go", "main.go"}),
		"absolute-path":  updateDocumentWithPaths(t, built.IndexIdentity, []string{"/main.go"}),
		"backslash-path": updateDocumentWithPaths(t, built.IndexIdentity, []string{`dir\\main.go`}),
		"traversal-path": updateDocumentWithPaths(t, built.IndexIdentity, []string{"../main.go"}),
		"null-paths":     updateDocumentWithPaths(t, built.IndexIdentity, nil),
	}
	for name, contents := range cases {
		t.Run(name, func(t *testing.T) {
			opened = 0
			writeUpdateDocument(t, state, contents)
			result, err := engine.Execute(context.Background(), request)
			if err != nil {
				t.Fatal(err)
			}
			if result.Status != wire.Stale || result.NextSafeAction != "rebuild-index" || opened != 0 {
				t.Fatalf("result = %#v source opens=%d", result, opened)
			}
		})
	}
	mutations := []struct {
		name            string
		mutate          func(map[string]any)
		recomputeLevel0 bool
	}{
		{name: "prior-index", mutate: func(v map[string]any) { v["prior_index_identity"] = testSHA2 }, recomputeLevel0: true},
		{name: "before-repository", mutate: func(v map[string]any) { v["before_repository_identity"] = testSHA2 }, recomputeLevel0: true},
		{name: "before-worktree", mutate: func(v map[string]any) { v["before_worktree_identity"] = testSHA2 }, recomputeLevel0: true},
		{name: "before-head", mutate: func(v map[string]any) { v["before_committed_head"] = "abcdef0123456789abcdef0123456789abcdef01" }, recomputeLevel0: true},
		{name: "before-dirty", mutate: func(v map[string]any) { v["before_dirty_overlay_fingerprint"] = testSHA2 }, recomputeLevel0: true},
		{name: "after-repository", mutate: func(v map[string]any) { v["after_repository_identity"] = engineSHA }, recomputeLevel0: true},
		{name: "after-worktree", mutate: func(v map[string]any) { v["after_worktree_identity"] = engineSHA }, recomputeLevel0: true},
		{name: "after-head", mutate: func(v map[string]any) { v["after_committed_head"] = "0123456789abcdef0123456789abcdef01234567" }, recomputeLevel0: true},
		{name: "after-dirty", mutate: func(v map[string]any) { v["after_dirty_overlay_fingerprint"] = engineSHA }, recomputeLevel0: true},
		{name: "wrong-level0", mutate: func(v map[string]any) { v["level0_change_manifest_identity"] = testSHA2 }},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			var value map[string]any
			if err := json.Unmarshal(valid, &value); err != nil {
				t.Fatal(err)
			}
			mutation.mutate(value)
			var contents []byte
			var err error
			if mutation.recomputeLevel0 {
				contents, err = recomputeUpdateDocumentIdentity(value)
			} else {
				contents, err = json.Marshal(value)
			}
			if err != nil {
				t.Fatal(err)
			}
			opened = 0
			writeUpdateDocument(t, state, contents)
			result, executeErr := engine.Execute(context.Background(), request)
			if executeErr != nil {
				t.Fatal(executeErr)
			}
			if result.Status != wire.Stale || result.NextSafeAction != "rebuild-index" || opened != 0 {
				t.Fatalf("binding result = %#v source opens=%d", result, opened)
			}
		})
	}
	t.Run("changed-path-limit", func(t *testing.T) {
		paths := make([]string, productionLimits().MaximumChangedPaths+1)
		for index := range paths {
			paths[index] = fmt.Sprintf("files/%05d.go", index)
		}
		writeUpdateDocument(t, state, updateDocumentWithPaths(t, built.IndexIdentity, paths))
		opened = 0
		result, err := engine.Execute(context.Background(), request)
		if err != nil {
			t.Fatal(err)
		}
		if result.Status != wire.Stale || result.NextSafeAction != "rebuild-index" || opened != 0 {
			t.Fatalf("limit result = %#v source opens=%d", result, opened)
		}
	})
}

func TestUpdateReplacesOnlyDeclaredRecordsAndMatchesCleanBuild(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "old.go"), []byte("package sample\nfunc Old() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	if err := os.Remove(filepath.Join(repository, "old.go")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "new.go"), []byte("package sample\nfunc New() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Changed() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocumentWithPaths(t, built.IndexIdentity, []string{"main.go", "new.go", "old.go"}))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead = "abcdef0123456789abcdef0123456789abcdef01"
	request.Request.DirtyOverlayFingerprint = testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.Freshness != "exact" || result.IndexIdentity == nil || result.NextSafeAction != "use-index" {
		t.Fatalf("update = %#v", result)
	}
	loaded, err := engine.dependencies.Load(context.Background(), mustRoots(t, request), *result.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range loaded.Records {
		if record.Path == "old.go" {
			t.Fatalf("deleted record retained: %#v", record)
		}
	}
	cleanState := filepath.Join(t.TempDir(), "clean-state")
	clean := mustBuildForUpdate(t, engine, repository, cleanState)
	cleanSnapshot, err := engine.dependencies.Load(context.Background(), mustRoots(t, controlEnvelope(wire.StatusOperation, repository, cleanState, clean.IndexIdentity)), *clean.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Manifest.SemanticDigest != cleanSnapshot.Manifest.SemanticDigest {
		t.Fatalf("semantic digest mismatch: %s != %s", loaded.Manifest.SemanticDigest, cleanSnapshot.Manifest.SemanticDigest)
	}
	if loaded.Manifest.SourceBindingDigest != cleanSnapshot.Manifest.SourceBindingDigest {
		t.Fatalf("source binding mismatch: %s != %s", loaded.Manifest.SourceBindingDigest, cleanSnapshot.Manifest.SourceBindingDigest)
	}
}

func TestUpdateHandlesSupportedToUnsupportedAndUnsupportedToSupported(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "switch.go"), []byte("package sample\nfunc Switch() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	if err := os.Rename(filepath.Join(repository, "switch.go"), filepath.Join(repository, "switch.txt")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "promoted.go"), []byte("package sample\nfunc Promoted() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocumentWithPaths(t, built.IndexIdentity, []string{"promoted.go", "switch.go", "switch.txt"}))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready {
		t.Fatalf("transition result = %#v", result)
	}
	loaded, err := engine.dependencies.Load(context.Background(), mustRoots(t, request), *result.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range loaded.Records {
		if record.Path == "switch.go" || record.Path == "switch.txt" {
			t.Fatalf("transition retained stale record: %#v", record)
		}
	}
}

func TestUpdateRejectsWrongBindingBeforeDeclaredSourceOpen(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	opened := 0
	original := engine.dependencies.OpenFile
	engine.dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opened++
		return original(roots, relative, maximum)
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || opened != 3 { // initial, preflight, and final-publication reads of exactly one declared source.
		t.Fatalf("valid source-open sequence = %#v opens=%d", result, opened)
	}
	opened = 0
	var wrong map[string]any
	if err := json.Unmarshal(updateDocument(t, built.IndexIdentity, "main.go"), &wrong); err != nil {
		t.Fatal(err)
	}
	wrong["prior_index_identity"] = testSHA2
	contents, err := recomputeUpdateDocumentIdentity(wrong)
	if err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, contents)
	result, err = engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || opened != 0 { // binding refusal precedes every repository source open.
		t.Fatalf("wrong prior binding opened declared source: %#v opens=%d", result, opened)
	}
}

func TestUpdatePreservesCurrentOnParseCancellationAndPublicationFailure(t *testing.T) {
	for name, arrange := range map[string]func(*Engine){
		"parse-failure": func(engine *Engine) {
			original := engine.dependencies.Extract
			engine.dependencies.Extract = func(ctx context.Context, file boundary.StableFile) ([]model.Record, extract.Report) {
				records, report := original(ctx, file)
				report.ParseFailures = 1
				return records, report
			}
		},
		"publication-failure": func(engine *Engine) {
			engine.dependencies.BuildWithBarrier = func(_ context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, _ func() error) (store.Snapshot, error) {
				return store.BuildWithFaults(roots, manifest, records, store.Faults{BeforeCurrentRename: errors.New("injected publication failure")})
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			repository, state := controlRoots(t)
			base := New(ProductionDependencies())
			built := mustBuildForUpdate(t, base, repository, state)
			before := inspectForUpdate(t, base, repository, state)
			if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Changed() {}\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
			candidate := New(base.dependencies)
			arrange(candidate)
			request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
			request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
			request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
			result, err := candidate.Execute(context.Background(), request)
			if err != nil {
				t.Fatal(err)
			}
			if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
				t.Fatalf("result = %#v", result)
			}
			after := inspectForUpdate(t, base, repository, state)
			if after.GenerationIdentity != before.GenerationIdentity {
				t.Fatalf("CURRENT changed after %s: %s != %s", name, after.GenerationIdentity, before.GenerationIdentity)
			}
		})
	}

	t.Run("cancellation", func(t *testing.T) {
		repository, state := controlRoots(t)
		engine := New(ProductionDependencies())
		built := mustBuildForUpdate(t, engine, repository, state)
		before := inspectForUpdate(t, engine, repository, state)
		writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
		request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
		request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
		request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		result, err := engine.Execute(ctx, request)
		if !errors.Is(err, context.Canceled) || result.SchemaVersion != "" {
			t.Fatalf("cancellation = %#v, %v", result, err)
		}
		after := inspectForUpdate(t, engine, repository, state)
		if after.GenerationIdentity != before.GenerationIdentity {
			t.Fatalf("CURRENT changed on cancellation: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
		}
	})
}

func TestUpdateReportsDeclaredLocalityAndNoOp(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	var counters model.WorkCounters
	engine.dependencies.ObserveUpdateCounters = func(got model.WorkCounters) { counters = got }
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || counters.ChangedPaths != 0 || counters.OpenedRepositoryFiles != 0 || counters.ParsedRepositoryFiles != 0 {
		t.Fatalf("no-op result/counters = %#v %#v", result, counters)
	}
}

func TestUpdateDoesNotUseBuildInventoryForUndeclaredSources(t *testing.T) {
	repository, state := controlRoots(t)
	for _, name := range []string{"untouched-a.go", "untouched-b.go"} {
		if err := os.WriteFile(filepath.Join(repository, name), []byte("package sample\nfunc Untouched() {}\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	loaded, err := base.dependencies.Load(context.Background(), mustRoots(t, controlEnvelope(wire.StatusOperation, repository, state, built.IndexIdentity)), *built.IndexIdentity)
	if err != nil {
		t.Fatalf("catalog load = %v", err)
	}
	if len(loaded.Manifest.SourceCatalog.Paths) != 3 {
		t.Fatalf("catalog paths = %#v", loaded.Manifest.SourceCatalog)
	}
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Changed() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	candidate.dependencies.Collect = func(_ boundary.Roots, mode inventory.Mode) (inventory.Result, error) {
		t.Fatalf("update used inventory mode %q", mode)
		return inventory.Result{}, nil
	}
	opened := []string{}
	var counters model.WorkCounters
	candidate.dependencies.ObserveUpdateCounters = func(got model.WorkCounters) { counters = got }
	originalOpen := candidate.dependencies.OpenFile
	candidate.dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opened = append(opened, relative)
		return originalOpen(roots, relative, maximum)
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	expectedBytes := int64(len("package sample\nfunc Changed() {}\n")) * 3
	if result.Status != wire.Ready || !slices.Equal(opened, []string{"main.go", "main.go", "main.go"}) || counters.OpenedRepositoryFiles != 3 || counters.ReadRepositoryBytes != expectedBytes {
		t.Fatalf("update result/open set/counters = %#v %#v %#v", result, opened, counters)
	}
}

func TestUpdateUnsafeDeclaredPathIsNotTreatedAsDeletion(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	before := inspectForUpdate(t, engine, repository, state)
	if err := os.Remove(filepath.Join(repository, "main.go")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("elsewhere.go", filepath.Join(repository, "main.go")); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("unsafe path result = %#v", result)
	}
	after := inspectForUpdate(t, engine, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed for unsafe path: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateRejectsCreateAfterDeclaredMissingBeforePublication(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	before := inspectForUpdate(t, engine, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "appeared.go"))
	candidate := New(engine.dependencies)
	opened := 0
	original := candidate.dependencies.OpenFile
	candidate.dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opened++
		file, err := original(roots, relative, maximum)
		if relative == "appeared.go" && opened == 1 && errors.Is(err, boundary.ErrRepositoryPathNotFound) {
			if writeErr := os.WriteFile(filepath.Join(repository, relative), []byte("package sample\nfunc Appeared() {}\n"), 0o600); writeErr != nil {
				t.Fatal(writeErr)
			}
		}
		return file, err
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" || opened < 2 {
		t.Fatalf("create-after-missing update = %#v opens=%d", result, opened)
	}
	after := inspectForUpdate(t, engine, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after create-after-missing: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateMatchesCleanBuildForExcludedPathTransitions(t *testing.T) {
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	for name, contents := range map[string][]byte{
		".gitignore":             []byte("ignored.go\n"),
		"ignored.go":             []byte("package sample\nfunc Ignored() {}\n"),
		"generated/file.go":      []byte("package sample\nfunc Generated() {}\n"),
		"vendor/library.go":      []byte("package sample\nfunc Vendor() {}\n"),
		"generated.generated.go": []byte("package sample\nfunc SuffixGenerated() {}\n"),
		"binary.go":              []byte("package sample\x00\n"),
		"oversized.go":           append([]byte("package sample\n"), make([]byte, productionLimits().MaximumSourceFileBytes)...),
	} {
		location := filepath.Join(repository, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(location), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(location, contents, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	paths := []string{".gitignore", "binary.go", "generated.generated.go", "generated/file.go", "ignored.go", "oversized.go", "vendor/library.go"}
	writeUpdateDocument(t, state, updateDocumentWithPaths(t, built.IndexIdentity, paths))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.IndexIdentity == nil {
		t.Fatalf("excluded transitions update = %#v", result)
	}
	updated, err := engine.dependencies.Load(context.Background(), mustRoots(t, request), *result.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	cleanState := filepath.Join(t.TempDir(), "clean-state")
	cleanResult := mustBuildForUpdateAfterBinding(t, engine, repository, cleanState)
	cleanEnvelope := controlEnvelope(wire.StatusOperation, repository, cleanState, cleanResult.IndexIdentity)
	clean, err := engine.dependencies.Load(context.Background(), mustRoots(t, cleanEnvelope), *cleanResult.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(updated.Records, clean.Records) || !reflect.DeepEqual(updated.Postings, clean.Postings) || !reflect.DeepEqual(updated.Manifest.Coverage, clean.Manifest.Coverage) || updated.Manifest.RecordCount != clean.Manifest.RecordCount || updated.Manifest.PostingCount != clean.Manifest.PostingCount || updated.Manifest.SemanticDigest != clean.Manifest.SemanticDigest || updated.Manifest.SourceBindingDigest != clean.Manifest.SourceBindingDigest {
		t.Fatalf("incremental excluded transitions diverged:\nupdated=%#v\nclean=%#v", updated.Manifest, clean.Manifest)
	}
}

func TestUpdateRemovesVanishedAncestorExclusionExactlyLikeCleanBuild(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.MkdirAll(filepath.Join(repository, "vendor"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "vendor", "library.go"), []byte("package vendor\nfunc Library() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	if err := os.Remove(filepath.Join(repository, "vendor", "library.go")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(repository, "vendor")); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "vendor/library.go"))
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := engine.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.IndexIdentity == nil {
		t.Fatalf("nested excluded deletion update = %#v", result)
	}
	updated, err := engine.dependencies.Load(context.Background(), mustRoots(t, request), *result.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	cleanState := filepath.Join(t.TempDir(), "clean-state")
	cleanResult := mustBuildForUpdateAfterBinding(t, engine, repository, cleanState)
	clean, err := engine.dependencies.Load(context.Background(), mustRoots(t, controlEnvelope(wire.StatusOperation, repository, cleanState, cleanResult.IndexIdentity)), *cleanResult.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(updated.Manifest.SourceCatalog, clean.Manifest.SourceCatalog) || !reflect.DeepEqual(updated.Manifest.Coverage, clean.Manifest.Coverage) || updated.Manifest.SourceBindingDigest != clean.Manifest.SourceBindingDigest {
		t.Fatalf("nested exclusion deletion diverged:\nupdated=%#v\nclean=%#v", updated.Manifest, clean.Manifest)
	}
}

func TestUpdateRetainsExcludedAncestorWhenSiblingStillExists(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.MkdirAll(filepath.Join(repository, "vendor"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"changed.go", "retained.go"} {
		if err := os.WriteFile(filepath.Join(repository, "vendor", name), []byte("package vendor\nfunc Retained() {}\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	if err := os.Remove(filepath.Join(repository, "vendor", "changed.go")); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "vendor/changed.go"))
	request := validUpdateEnvelope(repository, state, built.IndexIdentity)
	result, err := engine.Execute(context.Background(), request)
	if err != nil || result.Status != wire.Ready || result.IndexIdentity == nil {
		t.Fatalf("retained ancestor update = %#v, %v", result, err)
	}
	updated, err := engine.dependencies.Load(context.Background(), mustRoots(t, request), *result.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	cleanState := filepath.Join(t.TempDir(), "clean-state")
	cleanResult := mustBuildForUpdateAfterBinding(t, engine, repository, cleanState)
	clean := mustLoadUpdateSnapshot(t, engine, repository, cleanState, cleanResult.IndexIdentity)
	if !reflect.DeepEqual(updated.Manifest.SourceCatalog, clean.Manifest.SourceCatalog) || !reflect.DeepEqual(updated.Records, clean.Records) || !reflect.DeepEqual(updated.Postings, clean.Postings) || !reflect.DeepEqual(updated.Manifest.Coverage, clean.Manifest.Coverage) || updated.Manifest.RecordCount != clean.Manifest.RecordCount || updated.Manifest.PostingCount != clean.Manifest.PostingCount || updated.Manifest.SourceBindingDigest != clean.Manifest.SourceBindingDigest || updated.Manifest.SemanticDigest != clean.Manifest.SemanticDigest || updated.Manifest.IndexIdentity != clean.Manifest.IndexIdentity || updated.Manifest.GenerationIdentity != clean.Manifest.GenerationIdentity || !reflect.DeepEqual(result.Warnings, cleanResult.Warnings) || result.IndexIdentity == nil || cleanResult.IndexIdentity == nil || *result.IndexIdentity != *cleanResult.IndexIdentity {
		t.Fatalf("retained ancestor diverged:\nupdated=%#v\nclean=%#v", updated.Manifest, clean.Manifest)
	}
}

func TestUpdatePersistsExtractionWarningsAcrossChainedUpdates(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "warning.py"), []byte("class Registry:\n    pass\nvalue = globals()[name]\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine := New(ProductionDependencies())
	built := mustBuildForUpdate(t, engine, repository, state)
	if !hasWarning(built.Warnings, "python-dynamic-lookup") {
		t.Fatalf("clean build warnings = %v", built.Warnings)
	}
	current := built.IndexIdentity
	var final wire.Result
	for step, body := range []string{"package sample\nfunc ChangedOnce() {}\n", "package sample\nfunc ChangedTwice() {}\n"} {
		if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		document := updateDocument(t, current, "main.go")
		if step != 0 {
			document = chainedUpdateDocument(t, current, "main.go")
		}
		writeUpdateDocument(t, state, document)
		request := validUpdateEnvelope(repository, state, current)
		request.Request.RequestIdentity = fmt.Sprintf("warning-chain-%d", step)
		result, err := engine.Execute(context.Background(), request)
		if err != nil || result.Status != wire.Ready || result.IndexIdentity == nil || !hasWarning(result.Warnings, "python-dynamic-lookup") {
			t.Fatalf("warning chain step %d = %#v, %v", step, result, err)
		}
		current, final = result.IndexIdentity, result
	}
	updated := mustLoadUpdateSnapshot(t, engine, repository, state, final.IndexIdentity)
	cleanState := filepath.Join(t.TempDir(), "clean-state")
	cleanResult := mustBuildForUpdateAfterBinding(t, engine, repository, cleanState)
	clean := mustLoadUpdateSnapshot(t, engine, repository, cleanState, cleanResult.IndexIdentity)
	if !reflect.DeepEqual(updated.Manifest.SourceCatalog, clean.Manifest.SourceCatalog) || !reflect.DeepEqual(updated.Records, clean.Records) || !reflect.DeepEqual(updated.Postings, clean.Postings) || !reflect.DeepEqual(updated.Manifest.Coverage, clean.Manifest.Coverage) || updated.Manifest.RecordCount != clean.Manifest.RecordCount || updated.Manifest.PostingCount != clean.Manifest.PostingCount || updated.Manifest.SemanticDigest != clean.Manifest.SemanticDigest || updated.Manifest.SourceBindingDigest != clean.Manifest.SourceBindingDigest || updated.Manifest.IndexIdentity != clean.Manifest.IndexIdentity || updated.Manifest.GenerationIdentity != clean.Manifest.GenerationIdentity || !reflect.DeepEqual(final.Warnings, cleanResult.Warnings) || final.IndexIdentity == nil || cleanResult.IndexIdentity == nil || *final.IndexIdentity != *cleanResult.IndexIdentity {
		t.Fatalf("warning chain diverged:\nupdated=%#v\nclean=%#v", updated.Manifest, clean.Manifest)
	}
}

func TestUpdatePublishesAValidReadyResultWhenFitFailsAfterPublication(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Changed() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	candidate.dependencies.Fit = func(context.Context, wire.Request, wire.Result) (wire.Result, error) {
		return wire.Result{}, errors.New("injected fit failure")
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	var encoded bytes.Buffer
	encodeErr := wire.EncodeResult(&encoded, result)
	if result.Status != wire.Ready || result.IndexIdentity == nil || encodeErr != nil {
		t.Fatalf("published fit failure = %#v encode=%v", result, encodeErr)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity == before.GenerationIdentity || after.IndexIdentity != *result.IndexIdentity {
		t.Fatalf("published generation/result mismatch: before=%#v after=%#v result=%#v", before, after, result)
	}
}

func TestUpdateFinalStoreBarrierRejectsMutationAfterPreparation(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	original := candidate.dependencies.BuildWithBarrier
	candidate.dependencies.BuildWithBarrier = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, barrier func() error) (store.Snapshot, error) {
		return original(ctx, roots, manifest, records, func() error {
			if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc ChangedAfterPreparation() {}\n"), 0o600); err != nil {
				return err
			}
			return barrier()
		})
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("final-barrier mutation result = %#v", result)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after final barrier mutation: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateSameGenerationFinalBarrierRejectsSourceMutation(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	var document map[string]any
	if err := json.Unmarshal(updateDocument(t, built.IndexIdentity, "main.go"), &document); err != nil {
		t.Fatal(err)
	}
	document["after_repository_identity"] = document["before_repository_identity"]
	document["after_worktree_identity"] = document["before_worktree_identity"]
	document["after_committed_head"] = document["before_committed_head"]
	document["after_dirty_overlay_fingerprint"] = document["before_dirty_overlay_fingerprint"]
	contents, err := recomputeUpdateDocumentIdentity(document)
	if err != nil {
		t.Fatal(err)
	}
	writeUpdateDocument(t, state, contents)
	candidate := New(base.dependencies)
	original := candidate.dependencies.BuildWithBarrier
	barrierCalls := 0
	candidate.dependencies.BuildWithBarrier = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, barrier func() error) (store.Snapshot, error) {
		return original(ctx, roots, manifest, records, func() error {
			barrierCalls++
			if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc MutatedAtSameGenerationBarrier() {}\n"), 0o600); err != nil {
				return err
			}
			return barrier()
		})
	}
	result, err := candidate.Execute(context.Background(), updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt")))
	if err != nil {
		t.Fatal(err)
	}
	if barrierCalls != 1 || result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("same-generation mutation barrier calls = %d, result = %#v", barrierCalls, result)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after same-generation mutation: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateFinalStoreBarrierRejectsSameByteReplacement(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	original := candidate.dependencies.BuildWithBarrier
	candidate.dependencies.BuildWithBarrier = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, barrier func() error) (store.Snapshot, error) {
		return original(ctx, roots, manifest, records, func() error {
			originalInfo, err := os.Stat(filepath.Join(repository, "main.go"))
			if err != nil {
				return err
			}
			replacement := filepath.Join(repository, "replacement.go")
			if err := os.WriteFile(replacement, []byte("package sample\nfunc Main() {}\n"), 0o600); err != nil {
				return err
			}
			if err := os.Chtimes(replacement, originalInfo.ModTime(), originalInfo.ModTime()); err != nil {
				return err
			}
			if err := os.Rename(replacement, filepath.Join(repository, "main.go")); err != nil {
				return err
			}
			return barrier()
		})
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("same-byte replacement result = %#v", result)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after same-byte replacement: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateOversizedWitnessUsesMaximumPlusOneAndRejectsReplacement(t *testing.T) {
	repository, state := controlRoots(t)
	maximum := sourceMaximum("main.go")
	contents := bytes.Repeat([]byte{'x'}, int(maximum)+2)
	if err := os.WriteFile(filepath.Join(repository, "main.go"), contents, 0o600); err != nil {
		t.Fatal(err)
	}
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	var counters model.WorkCounters
	candidate.dependencies.ObserveUpdateCounters = func(got model.WorkCounters) { counters = got }
	original := candidate.dependencies.BuildWithBarrier
	candidate.dependencies.BuildWithBarrier = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, barrier func() error) (store.Snapshot, error) {
		return original(ctx, roots, manifest, records, func() error {
			originalInfo, err := os.Stat(filepath.Join(repository, "main.go"))
			if err != nil {
				return err
			}
			replacement := filepath.Join(repository, "oversized-replacement.go")
			if err := os.WriteFile(replacement, contents, 0o600); err != nil {
				return err
			}
			if err := os.Chtimes(replacement, originalInfo.ModTime(), originalInfo.ModTime()); err != nil {
				return err
			}
			if err := os.Rename(replacement, filepath.Join(repository, "main.go")); err != nil {
				return err
			}
			return barrier()
		})
	}
	result, err := candidate.Execute(context.Background(), validUpdateEnvelope(repository, state, built.IndexIdentity))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("oversized replacement result = %#v", result)
	}
	wantBytes := int64(3) * (maximum + 1)
	if counters.OpenedRepositoryFiles != 3 || counters.ReadRepositoryBytes != wantBytes {
		t.Fatalf("oversized counters = %#v, want opens=3 bytes=%d", counters, wantBytes)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after oversized replacement: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func TestUpdateFinalStoreBarrierRejectsIgnoredControlReplacement(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, ".gitignore"), []byte("# first control witness\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	base := New(ProductionDependencies())
	built := mustBuildForUpdate(t, base, repository, state)
	before := inspectForUpdate(t, base, repository, state)
	writeUpdateDocument(t, state, updateDocument(t, built.IndexIdentity, "main.go"))
	candidate := New(base.dependencies)
	original := candidate.dependencies.BuildWithBarrier
	candidate.dependencies.BuildWithBarrier = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record, barrier func() error) (store.Snapshot, error) {
		return original(ctx, roots, manifest, records, func() error {
			if err := os.WriteFile(filepath.Join(repository, ".gitignore"), []byte("# replaced control witness\n"), 0o600); err != nil {
				return err
			}
			return barrier()
		})
	}
	request := updateEnvelope(repository, state, built.IndexIdentity, testPtr(".taf-update.txt"))
	request.Request.RepositoryIdentity, request.Request.WorktreeIdentity = testSHA2, testSHA2
	request.Request.CommittedHead, request.Request.DirtyOverlayFingerprint = "abcdef0123456789abcdef0123456789abcdef01", testSHA2
	result, err := candidate.Execute(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Error || result.NextSafeAction != "rebuild-index" {
		t.Fatalf("ignored-control replacement result = %#v", result)
	}
	after := inspectForUpdate(t, base, repository, state)
	if after.GenerationIdentity != before.GenerationIdentity {
		t.Fatalf("CURRENT changed after ignored-control replacement: %s != %s", after.GenerationIdentity, before.GenerationIdentity)
	}
}

func mustBuildForUpdate(t *testing.T, engine *Engine, repository, state string) wire.Result {
	t.Helper()
	result, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	if result.IndexIdentity == nil {
		t.Fatalf("build = %#v", result)
	}
	return result
}

func mustBuildForUpdateAfterBinding(t *testing.T, engine *Engine, repository, state string) wire.Result {
	t.Helper()
	envelope := controlEnvelope(wire.Build, repository, state, nil)
	envelope.Request.RepositoryIdentity, envelope.Request.WorktreeIdentity = testSHA2, testSHA2
	envelope.Request.CommittedHead = "abcdef0123456789abcdef0123456789abcdef01"
	envelope.Request.DirtyOverlayFingerprint = testSHA2
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.IndexIdentity == nil {
		t.Fatalf("build = %#v", result)
	}
	return result
}

func updateEnvelope(repository, state string, index, document *string) wire.Envelope {
	envelope := controlEnvelope(wire.Update, repository, state, index)
	envelope.ChangedPathsDocument = document
	return envelope
}

func validUpdateEnvelope(repository, state string, index *string) wire.Envelope {
	envelope := updateEnvelope(repository, state, index, testPtr(".taf-update.txt"))
	envelope.Request.RepositoryIdentity, envelope.Request.WorktreeIdentity = testSHA2, testSHA2
	envelope.Request.CommittedHead = "abcdef0123456789abcdef0123456789abcdef01"
	envelope.Request.DirtyOverlayFingerprint = testSHA2
	return envelope
}

func mustLoadUpdateSnapshot(t *testing.T, engine *Engine, repository, state string, index *string) store.Snapshot {
	t.Helper()
	envelope := controlEnvelope(wire.StatusOperation, repository, state, index)
	envelope.Request.RepositoryIdentity, envelope.Request.WorktreeIdentity = testSHA2, testSHA2
	envelope.Request.CommittedHead = "abcdef0123456789abcdef0123456789abcdef01"
	envelope.Request.DirtyOverlayFingerprint = testSHA2
	snapshot, err := engine.dependencies.Load(context.Background(), mustRoots(t, envelope), *index)
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func writeUpdateDocument(t *testing.T, state string, contents []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(state, ".taf-update.txt"), contents, 0o600); err != nil {
		t.Fatal(err)
	}
}

// recomputeUpdateDocumentIdentity keeps negative binding tests structurally
// valid all the way through Level 0 validation, so their refusal proves the
// intended before/after binding seam rather than a stale manifest digest.
func recomputeUpdateDocumentIdentity(value map[string]any) ([]byte, error) {
	value["level0_change_manifest_identity"] = ""
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var decoded changeDocumentJSON
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		return nil, err
	}
	document := model.ChangeDocument{
		SchemaVersion: decoded.SchemaVersion, PriorIndexIdentity: decoded.PriorIndexIdentity,
		BeforeRepositoryIdentity: decoded.BeforeRepositoryIdentity, BeforeWorktreeIdentity: decoded.BeforeWorktreeIdentity,
		BeforeCommittedHead: decoded.BeforeCommittedHead, BeforeDirtyOverlayFingerprint: decoded.BeforeDirtyOverlayFingerprint,
		AfterRepositoryIdentity: decoded.AfterRepositoryIdentity, AfterWorktreeIdentity: decoded.AfterWorktreeIdentity,
		AfterCommittedHead: decoded.AfterCommittedHead, AfterDirtyOverlayFingerprint: decoded.AfterDirtyOverlayFingerprint,
		ChangedPaths: decoded.ChangedPaths,
	}
	value["level0_change_manifest_identity"] = changeManifestIdentity(document)
	return json.Marshal(value)
}

func updateDocument(t *testing.T, index *string, paths ...string) []byte {
	t.Helper()
	if paths == nil {
		paths = []string{}
	}
	return updateDocumentWithPaths(t, index, paths)
}

func updateDocumentWithPaths(t *testing.T, index *string, paths []string) []byte {
	t.Helper()
	value := map[string]any{
		"schema_version": "1", "prior_index_identity": *index,
		"before_repository_identity": engineSHA, "before_worktree_identity": engineSHA,
		"before_committed_head": "0123456789abcdef0123456789abcdef01234567", "before_dirty_overlay_fingerprint": engineSHA,
		"after_repository_identity": testSHA2, "after_worktree_identity": testSHA2,
		"after_committed_head": "abcdef0123456789abcdef0123456789abcdef01", "after_dirty_overlay_fingerprint": testSHA2,
		"changed_paths": paths,
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(append([]byte("taf-level0-change-manifest-v1\x00"), canonical...))
	value["level0_change_manifest_identity"] = "sha256:" + hex.EncodeToString(digest[:])
	output, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return output
}

func chainedUpdateDocument(t *testing.T, index *string, paths ...string) []byte {
	t.Helper()
	var value map[string]any
	if err := json.Unmarshal(updateDocument(t, index, paths...), &value); err != nil {
		t.Fatal(err)
	}
	value["before_repository_identity"] = testSHA2
	value["before_worktree_identity"] = testSHA2
	value["before_committed_head"] = "abcdef0123456789abcdef0123456789abcdef01"
	value["before_dirty_overlay_fingerprint"] = testSHA2
	contents, err := recomputeUpdateDocumentIdentity(value)
	if err != nil {
		t.Fatal(err)
	}
	return contents
}

func mustRoots(t *testing.T, envelope wire.Envelope) *boundary.Roots {
	t.Helper()
	roots, err := boundary.ValidateRoots(envelope)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = roots.Close() })
	return &roots
}

func inspectForUpdate(t *testing.T, engine *Engine, repository, state string) store.Status {
	t.Helper()
	status, err := engine.dependencies.Inspect(context.Background(), mustRoots(t, controlEnvelope(wire.StatusOperation, repository, state, testPtr(engineSHA))))
	if err != nil {
		t.Fatal(err)
	}
	return status
}
