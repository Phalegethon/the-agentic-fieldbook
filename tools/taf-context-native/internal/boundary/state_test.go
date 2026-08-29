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
	if err := staging.VerifyFile("index.bin", []byte("index")); err != nil {
		t.Fatalf("VerifyFile exact bytes: %v", err)
	}
	if err := staging.VerifyFile("index.bin", []byte("other")); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("VerifyFile mismatch = %v, want ErrUnsafeRoot", err)
	}
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

func TestStateControlMissingNestedParentUsesStateSentinel(t *testing.T) {
	roots := makeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	_, err := roots.OpenStateControlFile("missing/control.json", 1024)
	if !errors.Is(err, ErrStateEntryNotFound) || errors.Is(err, ErrRepositoryPathNotFound) {
		t.Fatalf("missing state parent error = %v", err)
	}
}

func TestStateControlReaderRejectsHostileEntries(t *testing.T) {
	writeControl := func(t *testing.T, roots *Roots, relative string, contents []byte, mode os.FileMode) string {
		t.Helper()
		location := filepath.Join(roots.State, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(location), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(location, contents, mode); err != nil {
			t.Fatal(err)
		}
		return location
	}
	for name, arrange := range map[string]func(*testing.T, *Roots){
		"final-symlink": func(t *testing.T, roots *Roots) {
			location := filepath.Join(roots.State, "control.json")
			if err := os.Symlink(filepath.Join(t.TempDir(), "outside"), location); err != nil {
				t.Fatal(err)
			}
		},
		"intermediate-symlink": func(t *testing.T, roots *Roots) {
			if err := os.Symlink(t.TempDir(), filepath.Join(roots.State, "controls")); err != nil {
				t.Fatal(err)
			}
		},
		"hardlink": func(t *testing.T, roots *Roots) {
			outside := filepath.Join(t.TempDir(), "outside.json")
			if err := os.WriteFile(outside, []byte("{}"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Link(outside, filepath.Join(roots.State, "control.json")); err != nil {
				t.Fatal(err)
			}
		},
		"insecure-permissions": func(t *testing.T, roots *Roots) {
			location := writeControl(t, roots, "control.json", []byte("{}"), 0o600)
			if err := os.Chmod(location, 0o644); err != nil {
				t.Fatal(err)
			}
		},
		"oversized": func(t *testing.T, roots *Roots) {
			writeControl(t, roots, "control.json", []byte("oversized"), 0o600)
		},
	} {
		t.Run(name, func(t *testing.T) {
			roots := makeRoots(t)
			if err := roots.EnsureState(); err != nil {
				t.Fatal(err)
			}
			arrange(t, &roots)
			relative, maximum := "control.json", int64(1024)
			if name == "intermediate-symlink" {
				relative = "controls/control.json"
			}
			if name == "oversized" {
				maximum = 3
			}
			if _, err := roots.OpenStateControlFile(relative, maximum); !errors.Is(err, ErrUnsafeRoot) {
				t.Fatalf("OpenStateControlFile(%q) error = %v, want ErrUnsafeRoot", relative, err)
			}
		})
	}

	t.Run("stat-open-read-replacement", func(t *testing.T) {
		roots := makeRoots(t)
		if err := roots.EnsureState(); err != nil {
			t.Fatal(err)
		}
		location := writeControl(t, &roots, "control.json", []byte("old"), 0o600)
		replacement := filepath.Join(roots.State, "replacement.json")
		if err := os.WriteFile(replacement, []byte("new"), 0o600); err != nil {
			t.Fatal(err)
		}
		stateControlBeforeReadHook = func() {
			if err := os.Rename(replacement, location); err != nil {
				t.Fatal(err)
			}
		}
		t.Cleanup(func() { stateControlBeforeReadHook = nil })
		if _, err := roots.OpenStateControlFile("control.json", 1024); !errors.Is(err, ErrStateEntryChanged) {
			t.Fatalf("replacement error = %v, want ErrStateEntryChanged", err)
		}
	})

	roots := makeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	for _, relative := range []string{"../escape.json", "/absolute.json", `back\\slash.json`} {
		if _, err := roots.OpenStateControlFile(relative, 1024); !errors.Is(err, ErrUnsafePath) {
			t.Fatalf("lexical control path %q error = %v", relative, err)
		}
	}
}
