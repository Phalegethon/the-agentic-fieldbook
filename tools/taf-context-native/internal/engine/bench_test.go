package engine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// TestBenchUpdateOnRealRepository times ten chained Update calls against a
// copy of a real repository (defaulting to this checkout), each touching a
// single Python file. It is opt-in and prints nothing unless
// TAF_ENGINE_BENCH=1. The timed edits run against a copy so the real
// checkout is never touched.
func TestBenchUpdateOnRealRepository(t *testing.T) {
	if os.Getenv("TAF_ENGINE_BENCH") != "1" {
		t.Skip("set TAF_ENGINE_BENCH=1 to time update on a real repository")
	}
	source := os.Getenv("TAF_BENCH_REPOSITORY")
	if source == "" {
		source = filepath.Join("..", "..", "..", "..")
	}
	source, _ = filepath.Abs(source)
	if _, err := os.Lstat(filepath.Join(source, ".git")); err != nil {
		t.Skipf("no repository at TAF_BENCH_REPOSITORY: %v", err)
	}
	// Work on a copy so the timed edits never touch the real checkout.
	repository := filepath.Join(t.TempDir(), "repository")
	copyTree(t, source, repository)
	state := filepath.Join(t.TempDir(), "state")
	engine := New(ProductionDependencies())
	var counters model.WorkCounters
	engine.dependencies.ObserveUpdateCounters = func(got model.WorkCounters) { counters = got }
	built := mustBuildForUpdate(t, engine, repository, state)
	target := filepath.Join(repository, "tools", "taf-context", "taf_context", "state_paths.py")
	original, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	index := built.IndexIdentity
	durations := make([]time.Duration, 0, 10)
	for n := range 10 {
		edited := append(append([]byte{}, original...), []byte(fmt.Sprintf("\n\ndef bench_marker_%d():\n    return %d\n", n, n))...)
		if err := os.WriteFile(target, edited, 0o600); err != nil {
			t.Fatal(err)
		}
		writeUpdateDocument(t, state, benchDocument(t, index, n, "tools/taf-context/taf_context/state_paths.py"))
		envelope := benchUpdateEnvelope(repository, state, index, n)
		began := time.Now()
		result, err := engine.Execute(context.Background(), envelope)
		durations = append(durations, time.Since(began))
		if err != nil || (result.Status != wire.Ready && result.Status != wire.Partial) || result.IndexIdentity == nil {
			t.Fatalf("update %d = %#v, %v", n, result, err)
		}
		index = result.IndexIdentity
	}
	slices.Sort(durations)
	t.Logf("update median=%s min=%s max=%s counters=%+v", durations[5], durations[0], durations[9], counters)
}

// benchDocument builds the change document for iteration n of the bench
// loop. Every iteration is an uncommitted edit: repository identity,
// worktree identity, and committed head never move, only the dirty overlay
// fingerprint advances. Iteration 0's before-binding is the build binding
// from controlEnvelope (engineSHA/engineSHA/committed-head/engineSHA);
// iteration n>0's before-binding is iteration n-1's after-binding, matching
// what update.go records into manifest.Binding on success.
func benchDocument(t *testing.T, index *string, n int, path string) []byte {
	t.Helper()
	beforeDirty := engineSHA
	if n > 0 {
		beforeDirty = "sha256:" + fmt.Sprintf("%064x", n)
	}
	afterDirty := "sha256:" + fmt.Sprintf("%064x", n+1)
	value := map[string]any{
		"schema_version": "1", "prior_index_identity": *index,
		"before_repository_identity": engineSHA, "before_worktree_identity": engineSHA,
		"before_committed_head": "0123456789abcdef0123456789abcdef01234567", "before_dirty_overlay_fingerprint": beforeDirty,
		"after_repository_identity": engineSHA, "after_worktree_identity": engineSHA,
		"after_committed_head": "0123456789abcdef0123456789abcdef01234567", "after_dirty_overlay_fingerprint": afterDirty,
		"changed_paths": []string{path},
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

// benchUpdateEnvelope builds the request for iteration n, matching the
// dirty-overlay-only advance that benchDocument declares.
func benchUpdateEnvelope(repository, state string, index *string, n int) wire.Envelope {
	envelope := updateEnvelope(repository, state, index, testPtr(".taf-update.txt"))
	envelope.Request.DirtyOverlayFingerprint = "sha256:" + fmt.Sprintf("%064x", n+1)
	return envelope
}

// copyTree copies source into destination, skipping the contents of any
// .git directory (or file, for a worktree pointer) but leaving an empty
// .git marker directory behind so boundary.ValidateRoots still finds
// repository metadata. Everything else is copied byte-for-byte with its
// original mode.
func copyTree(t *testing.T, source, destination string) {
	t.Helper()
	err := filepath.WalkDir(source, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if relative == "." {
			return os.MkdirAll(destination, 0o700)
		}
		if relative == ".git" {
			if mkdirErr := os.Mkdir(filepath.Join(destination, ".git"), 0o700); mkdirErr != nil {
				return mkdirErr
			}
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		target := filepath.Join(destination, relative)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if !entry.Type().IsRegular() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, info.Mode().Perm())
	})
	if err != nil {
		t.Fatal(err)
	}
}
