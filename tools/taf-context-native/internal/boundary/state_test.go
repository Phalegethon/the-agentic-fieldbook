package boundary

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestStateDirectoryCapabilitySurvivesLexicalRootReplacement(t *testing.T) {
	repository := makeRepository(t)
	base := t.TempDir()
	state := filepath.Join(base, "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	roots, err := ValidateRoots(validEnvelope(repository, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()

	retained := filepath.Join(base, "retained")
	if err := os.Rename(state, retained); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	directory, err := roots.OpenStateDirectory("")
	if err != nil {
		t.Fatal(err)
	}
	defer directory.Close()
	if err := directory.CreateDirectory("generations"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(retained, "generations")); err != nil {
		t.Fatalf("retained state was not mutated: %v", err)
	}
	if _, err := os.Stat(filepath.Join(state, "generations")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("replacement path was mutated: %v", err)
	}
}

func TestStateDirectoryRejectsUnsafeEntriesAndSupportsAtomicRename(t *testing.T) {
	roots := makeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	directory, err := roots.OpenStateDirectory("")
	if err != nil {
		t.Fatal(err)
	}
	defer directory.Close()
	if err := directory.CreateDirectory("generations"); err != nil {
		t.Fatal(err)
	}
	generations, err := directory.OpenDirectory("generations")
	if err != nil {
		t.Fatal(err)
	}
	defer generations.Close()
	if err := generations.CreateDirectory("staging"); err != nil {
		t.Fatal(err)
	}
	staging, err := generations.OpenDirectory("staging")
	if err != nil {
		t.Fatal(err)
	}
	file, err := staging.CreateFile("index.bin")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte("index")); err != nil {
		t.Fatal(err)
	}
	if err := file.Sync(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	opened, err := staging.OpenFile("index.bin")
	if err != nil {
		t.Fatal(err)
	}
	_ = opened.Close()
	if err := staging.Sync(); err != nil {
		t.Fatal(err)
	}
	if err := staging.Close(); err != nil {
		t.Fatal(err)
	}
	if err := generations.RenameNew("staging", "generation"); err != nil {
		t.Fatal(err)
	}
	if err := generations.Sync(); err != nil {
		t.Fatal(err)
	}

	current, err := directory.CreateFile(".CURRENT.next")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := current.Write([]byte("generation\n")); err != nil {
		t.Fatal(err)
	}
	if err := current.Close(); err != nil {
		t.Fatal(err)
	}
	if err := directory.ReplaceFile(".CURRENT.next", "CURRENT"); err != nil {
		t.Fatal(err)
	}
	if err := directory.Sync(); err != nil {
		t.Fatal(err)
	}
	if value, err := directory.ReadAtomicCurrent(int64(len("generation\n"))); err != nil || string(value) != "generation\n" {
		t.Fatalf("ReadAtomicCurrent = %q, %v", value, err)
	}
	if _, err := directory.ReadAtomicCurrent(int64(len("generation\n") + 1)); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("partial CURRENT error = %v, want ErrUnsafeRoot", err)
	}

	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(outside, filepath.Join(roots.State, "linked")); err != nil {
		t.Fatal(err)
	}
	if _, err := directory.OpenFile("linked"); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("hardlink error = %v, want ErrUnsafeRoot", err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(roots.State, "linked-dir")); err != nil {
		t.Fatal(err)
	}
	if _, err := directory.OpenDirectory("linked-dir"); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("symlink directory error = %v, want ErrUnsafeRoot", err)
	}
}

func TestStateDirectoryRejectsTraversalAndClosedCapabilities(t *testing.T) {
	roots := makeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	directory, err := roots.OpenStateDirectory("")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"../escape", "/absolute", `back\\slash`, "a/b", ""} {
		if err := directory.CreateDirectory(name); !errors.Is(err, ErrUnsafeRoot) {
			t.Fatalf("CreateDirectory(%q) error = %v", name, err)
		}
	}
	if err := directory.Close(); err != nil {
		t.Fatal(err)
	}
	if err := directory.Sync(); !errors.Is(err, ErrStateUnavailable) {
		t.Fatalf("closed Sync error = %v", err)
	}
}
