package boundary

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestValidateRootsRejectsOverlappingRoots(t *testing.T) {
	repo := makeRepository(t)
	for _, state := range []string{
		filepath.Join(repo, ".taf-state"),
		filepath.Dir(repo),
	} {
		_, err := ValidateRoots(validEnvelope(repo, state))
		if !errors.Is(err, ErrRootOverlap) {
			t.Fatalf("ValidateRoots(%q) error = %v, want ErrRootOverlap", state, err)
		}
	}
}

func TestValidateRootsRejectsSymlinkedRoots(t *testing.T) {
	repo := makeRepository(t)
	parent := t.TempDir()
	repoLink := filepath.Join(parent, "repository-link")
	mustSymlink(t, repo, repoLink)
	if _, err := ValidateRoots(validEnvelope(repoLink, filepath.Join(parent, "state"))); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("repository symlink error = %v, want ErrUnsafeRoot", err)
	}

	state := filepath.Join(parent, "state")
	mustSymlink(t, t.TempDir(), state)
	if _, err := ValidateRoots(validEnvelope(repo, state)); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("state symlink error = %v, want ErrUnsafeRoot", err)
	}
}

func TestValidateRootsRejectsInsecureStatePermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows does not provide POSIX permission bits")
	}
	repo := makeRepository(t)
	state := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(state, 0o750); err != nil {
		t.Fatal(err)
	}
	if _, err := ValidateRoots(validEnvelope(repo, state)); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("group-readable state error = %v, want ErrUnsafeRoot", err)
	}
	if err := os.Chmod(state, 0o707); err != nil {
		t.Fatal(err)
	}
	if _, err := ValidateRoots(validEnvelope(repo, state)); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("world-writable state error = %v, want ErrUnsafeRoot", err)
	}
}

func TestValidateRootsCreatesPrivateStateWithoutModifyingRepository(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows does not provide POSIX permission bits")
	}
	repo := makeRepository(t)
	state := filepath.Join(t.TempDir(), "state")
	roots, err := ValidateRoots(validEnvelope(repo, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if _, err := os.Stat(state); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("ValidateRoots created state: %v", err)
	}
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(state)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o700 {
		t.Fatalf("state permissions = %o, want 700", got)
	}
	if roots.Repository != repo || roots.State != state {
		t.Fatalf("roots = %#v, want canonical repository and state paths", roots)
	}
	git(t, repo, "diff", "--exit-code")
	if got := strings.TrimSpace(gitOutput(t, repo, "status", "--porcelain")); got != "" {
		t.Fatalf("repository was modified: %q", got)
	}
}

func TestRootsKeepRepositoryHandleAfterPathReplacement(t *testing.T) {
	repo := makeRepository(t)
	if err := os.WriteFile(filepath.Join(repo, "safe.go"), []byte("trusted"), 0o600); err != nil {
		t.Fatal(err)
	}
	roots, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state")))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	old := repo + "-trusted"
	if err := os.Rename(repo, old); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(repo, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "safe.go"), []byte("attacker"), 0o600); err != nil {
		t.Fatal(err)
	}
	file, err := roots.OpenRepositoryFile("safe.go", 1024)
	if err != nil {
		t.Fatal(err)
	}
	if string(file.Bytes) != "trusted" {
		t.Fatalf("read replaced repository path: %q", file.Bytes)
	}
}

func TestRootsKeepStateHandleAfterPathReplacement(t *testing.T) {
	repo := makeRepository(t)
	state := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	roots, err := ValidateRoots(validEnvelope(repo, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	trusted := state + "-trusted"
	outside := t.TempDir()
	if err := os.Rename(state, trusted); err != nil {
		t.Fatal(err)
	}
	mustSymlink(t, outside, state)
	file, err := roots.CreateStateFile("marker")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte("owner-only")); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(filepath.Join(trusted, "marker")); err != nil || string(got) != "owner-only" {
		t.Fatalf("trusted state write = %q, %v", got, err)
	}
	if _, err := os.Stat(filepath.Join(outside, "marker")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("write escaped through replacement state path: %v", err)
	}
}

func TestEnsureStateRejectsProtectedDirectoriesRenamedIntoMissingStatePath(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("renaming open directory handles is not portable to Windows")
	}
	for _, protected := range []string{"repository", "git-directory", "git-common-directory"} {
		for _, placement := range []string{"component", "terminal"} {
			t.Run(protected+"/"+placement, func(t *testing.T) {
				primary := makeRepository(t)
				repository := filepath.Join(t.TempDir(), "linked")
				git(t, primary, "worktree", "add", "--detach", repository, "HEAD")
				gitDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-dir")
				gitCommonDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
				target := map[string]string{
					"repository":           repository,
					"git-directory":        gitDirectory,
					"git-common-directory": gitCommonDirectory,
				}[protected]
				if err := os.Chmod(target, 0o700); err != nil {
					t.Fatal(err)
				}

				base := t.TempDir()
				state := filepath.Join(base, "protected")
				if placement == "component" {
					state = filepath.Join(state, "nested")
				}
				roots, err := ValidateRoots(validEnvelope(repository, state))
				if err != nil {
					t.Fatal(err)
				}
				defer roots.Close()
				before := snapshotDirectoryTree(t, target)
				if err := os.Rename(target, filepath.Join(base, "protected")); err != nil {
					t.Fatal(err)
				}

				ensureErr := roots.EnsureState()
				if ensureErr == nil && placement == "terminal" {
					file, createErr := roots.CreateStateFile("must-not-exist")
					if createErr == nil {
						_ = file.Close()
					}
				}
				after := snapshotDirectoryTree(t, filepath.Join(base, "protected"))
				if !errors.Is(ensureErr, ErrRootOverlap) {
					t.Errorf("EnsureState error = %v, want ErrRootOverlap", ensureErr)
				}
				if !reflect.DeepEqual(after, before) {
					t.Error("EnsureState mutated protected directory tree")
				}
			})
		}
	}
}

func TestStateFileTraversalRejectsProtectedDirectoriesMovedBelowState(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("renaming open directory handles is not portable to Windows")
	}
	for _, protected := range []string{"repository", "git-directory", "git-common-directory"} {
		t.Run(protected, func(t *testing.T) {
			primary := makeRepository(t)
			repository := filepath.Join(t.TempDir(), "linked")
			git(t, primary, "worktree", "add", "--detach", repository, "HEAD")
			gitDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-dir")
			gitCommonDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
			target := map[string]string{
				"repository":           repository,
				"git-directory":        gitDirectory,
				"git-common-directory": gitCommonDirectory,
			}[protected]
			if err := os.WriteFile(filepath.Join(target, "protected-sentinel"), []byte("repository bytes"), 0o600); err != nil {
				t.Fatal(err)
			}
			state := filepath.Join(t.TempDir(), "state")
			if err := os.Mkdir(state, 0o700); err != nil {
				t.Fatal(err)
			}
			roots, err := ValidateRoots(validEnvelope(repository, state))
			if err != nil {
				t.Fatal(err)
			}
			defer roots.Close()
			before := snapshotDirectoryTree(t, target)
			moved := filepath.Join(state, "protected")
			if err := os.Rename(target, moved); err != nil {
				t.Fatal(err)
			}

			operations := []struct {
				name string
				run  func() error
			}{
				{name: "open", run: func() error {
					file, err := roots.OpenStateFile("protected/protected-sentinel")
					if file != nil {
						_ = file.Close()
					}
					return err
				}},
				{name: "create", run: func() error {
					file, err := roots.CreateStateFile("protected/must-not-exist")
					if file != nil {
						_ = file.Close()
					}
					return err
				}},
				{name: "replace", run: func() error {
					return roots.ReplaceStateFile("protected/protected-sentinel", []byte("mutated"))
				}},
			}
			for _, operation := range operations {
				if err := operation.run(); !errors.Is(err, ErrRootOverlap) {
					t.Errorf("%s state file error = %v, want ErrRootOverlap", operation.name, err)
				}
			}
			after := snapshotDirectoryTree(t, moved)
			if !reflect.DeepEqual(after, before) {
				t.Error("state operations mutated protected directory tree")
			}
		})
	}
}

func TestCaptureRejectsRepositoryAndStateReplacementDuringValidation(t *testing.T) {
	for _, rootKind := range []string{"repository", "state"} {
		t.Run(rootKind, func(t *testing.T) {
			base := t.TempDir()
			repo := filepath.Join(base, "repository")
			if err := os.Mkdir(repo, 0o700); err != nil {
				t.Fatal(err)
			}
			git(t, repo, "init", "-q")
			state := filepath.Join(base, "state")
			if err := os.Mkdir(state, 0o700); err != nil {
				t.Fatal(err)
			}
			target := repo
			if rootKind == "state" {
				target = state
			}
			captureBeforeOpenHook = func(path string) {
				if path != target {
					return
				}
				trusted := target + "-trusted"
				if err := os.Rename(target, trusted); err != nil {
					t.Fatal(err)
				}
				if err := os.Mkdir(target, 0o700); err != nil {
					t.Fatal(err)
				}
			}
			t.Cleanup(func() { captureBeforeOpenHook = nil })
			if _, err := ValidateRoots(validEnvelope(repo, state)); !errors.Is(err, ErrUnsafeRoot) {
				t.Fatalf("replacement validation error = %v, want ErrUnsafeRoot", err)
			}
		})
	}
}

func TestCaptureRejectsParentAndGitMetadataReplacementDuringValidation(t *testing.T) {
	base := t.TempDir()
	repo := filepath.Join(base, "repository")
	if err := os.Mkdir(repo, 0o700); err != nil {
		t.Fatal(err)
	}
	git(t, repo, "init", "-q")
	state := filepath.Join(t.TempDir(), "state")
	captureBeforeOpenHook = func(path string) {
		if path != base {
			return
		}
		trusted := base + "-trusted"
		if err := os.Rename(base, trusted); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(base, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(filepath.Join(base, "repository"), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { captureBeforeOpenHook = nil })
	if _, err := ValidateRoots(validEnvelope(repo, state)); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("parent replacement error = %v, want ErrUnsafeRoot", err)
	}
}

func TestValidateRootsRejectsOnlyEndpointSpecificStateIdentityAliases(t *testing.T) {
	primary := makeRepository(t)
	repository := filepath.Join(t.TempDir(), "linked")
	git(t, primary, "worktree", "add", "--detach", repository, "HEAD")
	gitDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-dir")
	gitCommonDirectory := gitOutput(t, repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
	for name, endpoint := range map[string]string{
		"repository":           repository,
		"git-directory":        gitDirectory,
		"git-common-directory": gitCommonDirectory,
	} {
		t.Run(name, func(t *testing.T) {
			base := t.TempDir()
			alias := filepath.Join(base, "alias")
			control := filepath.Join(base, "same-volume-control")
			for _, path := range []string{alias, control} {
				if err := os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			}
			endpointMarker, err := os.Stat(endpoint)
			if err != nil {
				t.Fatal(err)
			}
			aliasMarker, err := os.Stat(alias)
			if err != nil {
				t.Fatal(err)
			}
			identityEqualHook = func(first, second os.FileInfo) bool {
				if os.SameFile(first, second) {
					return true
				}
				return (os.SameFile(first, endpointMarker) && os.SameFile(second, aliasMarker)) ||
					(os.SameFile(first, aliasMarker) && os.SameFile(second, endpointMarker))
			}
			t.Cleanup(func() { identityEqualHook = nil })

			controlRoots, err := ValidateRoots(validEnvelope(repository, control))
			if err != nil {
				t.Fatalf("non-overlapping same-volume state error = %v", err)
			}
			if err := controlRoots.Close(); err != nil {
				t.Fatal(err)
			}
			aliasedRoots, err := ValidateRoots(validEnvelope(repository, alias))
			if err == nil {
				_ = aliasedRoots.Close()
			}
			if !errors.Is(err, ErrRootOverlap) {
				t.Fatalf("state terminal alias of %s error = %v, want ErrRootOverlap", name, err)
			}
			identityEqualHook = nil
		})
	}
}

func TestDescendAndEnsureStateRejectReplacementAfterPreLstat(t *testing.T) {
	roots := makeRoots(t)
	if err := os.Mkdir(filepath.Join(roots.Repository, "nested"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(roots.Repository, "nested", "safe.go"), []byte("safe"), 0o600); err != nil {
		t.Fatal(err)
	}
	descendBeforeOpenHook = func(component string) {
		if component != "nested" {
			return
		}
		path := filepath.Join(roots.Repository, "nested")
		if err := os.Rename(path, path+"-trusted"); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { descendBeforeOpenHook = nil })
	if _, err := roots.OpenRepositoryFile("nested/safe.go", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("descend replacement error = %v, want ErrUnsafePath", err)
	}

	repo := makeRepository(t)
	base := t.TempDir()
	stateRoots, err := ValidateRoots(validEnvelope(repo, filepath.Join(base, "first", "second")))
	if err != nil {
		t.Fatal(err)
	}
	defer stateRoots.Close()
	stateEnsureBeforeOpenHook = func(component string) {
		if component != "first" {
			return
		}
		path := filepath.Join(base, "first")
		if err := os.Rename(path, path+"-trusted"); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { stateEnsureBeforeOpenHook = nil })
	if err := stateRoots.EnsureState(); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("EnsureState replacement error = %v, want ErrUnsafeRoot", err)
	}
}

func TestMacOSCaseAliasesAreRejectedWhenSupported(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("native macOS-only case-alias probe")
	}
	repo := makeRepository(t)
	alias := strings.ToUpper(repo)
	info, err := os.Stat(alias)
	if err != nil {
		t.Skip("filesystem is case-sensitive")
	}
	original, err := os.Stat(repo)
	if err != nil || !os.SameFile(info, original) {
		t.Skip("filesystem does not provide a case alias")
	}
	if _, err := ValidateRoots(validEnvelope(repo, alias)); !errors.Is(err, ErrRootOverlap) {
		t.Fatalf("case-alias state error = %v, want ErrRootOverlap", err)
	}
}

func TestMetadataRejectsReplacementAndWrongGrammar(t *testing.T) {
	repo := makeRepository(t)
	gitDirectory := filepath.Join(repo, ".git")
	separate := filepath.Join(repo, "separate")
	if err := os.Rename(gitDirectory, separate); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(gitDirectory, []byte("gitdir: separate\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	candidate := filepath.Join(repo, "metadata-replacement")
	if err := os.WriteFile(candidate, []byte("gitdir: attacker\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	metadataOpenHook = func(name string) {
		if name != ".git" {
			return
		}
		if err := os.Rename(candidate, gitDirectory); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { metadataOpenHook = nil })
	if _, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state"))); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("metadata replacement error = %v, want ErrUnsafeRoot", err)
	}
	metadataOpenHook = nil
	if err := os.WriteFile(gitDirectory, []byte("separate\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state"))); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("missing gitdir grammar error = %v, want ErrUnsafeRoot", err)
	}
}

func TestGitDiscoveryBindsInitialDotGitEntryIdentity(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("renaming open directory handles is not portable to Windows")
	}
	for _, metadataKind := range []string{"directory", "file"} {
		t.Run(metadataKind, func(t *testing.T) {
			repository := makeRepository(t)
			dotGit := filepath.Join(repository, ".git")
			originalRelative := "original-git-metadata"
			if metadataKind == "file" {
				separate := filepath.Join(repository, "separate-git-directory")
				if err := os.Rename(dotGit, separate); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(dotGit, []byte("gitdir: separate-git-directory\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			} else {
				originalRelative += "/HEAD"
			}
			trustedRoots, err := ValidateRoots(validEnvelope(repository, filepath.Join(t.TempDir(), "trusted-state")))
			if err != nil {
				t.Fatal(err)
			}
			defer trustedRoots.Close()

			originalMetadata := filepath.Join(repository, "original-git-metadata")
			replacement := filepath.Join(repository, "stable-replacement")
			if err := os.Mkdir(replacement, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(replacement, "HEAD"), []byte("replacement\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			before := snapshotDirectoryTree(t, dotGit)
			triggered := false
			gitDiscoveryBeforeOpenHook = func() {
				if triggered {
					return
				}
				triggered = true
				if err := os.Rename(dotGit, originalMetadata); err != nil {
					t.Fatal(err)
				}
				if err := os.Rename(replacement, dotGit); err != nil {
					t.Fatal(err)
				}
			}
			t.Cleanup(func() { gitDiscoveryBeforeOpenHook = nil })

			replacedRoots, err := ValidateRoots(validEnvelope(repository, filepath.Join(t.TempDir(), "replacement-state")))
			if err == nil {
				_ = replacedRoots.Close()
			}
			gitDiscoveryBeforeOpenHook = nil
			if !triggered {
				t.Fatal("Git discovery replacement hook was not reached")
			}
			if !errors.Is(err, ErrUnsafeRoot) {
				t.Fatalf("Git discovery replacement error = %v, want ErrUnsafeRoot", err)
			}
			if _, err := trustedRoots.OpenRepositoryFile(originalRelative, 1024); !errors.Is(err, ErrUnsafePath) {
				t.Fatalf("opened renamed original Git metadata through repository API: %v", err)
			}
			after := snapshotDirectoryTree(t, originalMetadata)
			if !reflect.DeepEqual(after, before) {
				t.Fatal("Git discovery or repository read mutated original metadata")
			}
		})
	}
}

func TestGitDiscoveryRejectsRelativeGitDirBaseReplacementAfterMetadataRead(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("renaming open directory handles is not portable to Windows")
	}
	fixture := makeRelativeGitMetadataRepository(t)
	original := snapshotDirectoryTree(t, fixture.base)
	trustedBase := fixture.base + "-trusted"
	attackerGitDirectory := fixture.gitDirectory
	triggered := false
	metadataTargetCaptureHook = func(name string) {
		if name != ".git" || triggered {
			return
		}
		triggered = true
		if err := os.Rename(fixture.base, trustedBase); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(fixture.repository, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(fixture.repository, ".git"), []byte("gitdir: ../control metadata with spaces/worktrees/git dir\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(attackerGitDirectory, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(attackerGitDirectory, "HEAD"), []byte("attacker\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { metadataTargetCaptureHook = nil })

	beforeFDs := openFDCountIfSupported(t)
	roots, err := ValidateRoots(validEnvelope(fixture.repository, filepath.Join(t.TempDir(), "state")))
	retained := roots.repositoryRoot != nil || roots.gitDirectoryRoot != nil || roots.gitCommonRoot != nil || roots.stateRoot != nil || roots.stateParent != nil
	_ = roots.Close()
	metadataTargetCaptureHook = nil
	if !triggered {
		t.Fatal("relative .git target-capture hook was not reached")
	}
	if !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("relative .git base replacement error = %v, want ErrUnsafeRoot", err)
	}
	if retained {
		t.Fatal("failed validation returned retained capabilities")
	}
	assertOpenFDCountUnchanged(t, beforeFDs)
	if after := snapshotDirectoryTree(t, trustedBase); !reflect.DeepEqual(after, original) {
		t.Fatal("relative .git discovery mutated original Git metadata")
	}
}

func TestGitDiscoveryRejectsRelativeCommonDirBaseReplacementAfterMetadataRead(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("renaming open directory handles is not portable to Windows")
	}
	fixture := makeRelativeGitMetadataRepository(t)
	original := snapshotDirectoryTree(t, fixture.controlBase)
	trustedControlBase := fixture.controlBase + "-trusted"
	attackerGitDirectory := fixture.gitDirectory
	attackerCommonDirectory := fixture.commonDirectory
	triggered := false
	metadataTargetCaptureHook = func(name string) {
		if name != "commondir" || triggered {
			return
		}
		triggered = true
		if err := os.Rename(fixture.controlBase, trustedControlBase); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(attackerGitDirectory, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(attackerGitDirectory, "HEAD"), []byte("attacker git\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(attackerCommonDirectory, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(attackerCommonDirectory, "config"), []byte("attacker common\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { metadataTargetCaptureHook = nil })

	beforeFDs := openFDCountIfSupported(t)
	roots, err := ValidateRoots(validEnvelope(fixture.repository, filepath.Join(t.TempDir(), "state")))
	retained := roots.repositoryRoot != nil || roots.gitDirectoryRoot != nil || roots.gitCommonRoot != nil || roots.stateRoot != nil || roots.stateParent != nil
	_ = roots.Close()
	metadataTargetCaptureHook = nil
	if !triggered {
		t.Fatal("relative commondir target-capture hook was not reached")
	}
	if !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("relative commondir base replacement error = %v, want ErrUnsafeRoot", err)
	}
	if retained {
		t.Fatal("failed validation returned retained capabilities")
	}
	assertOpenFDCountUnchanged(t, beforeFDs)
	if after := snapshotDirectoryTree(t, trustedControlBase); !reflect.DeepEqual(after, original) {
		t.Fatal("relative commondir discovery mutated original common metadata")
	}
}

func TestValidateRootsAcceptsRelativeGitTargetsWithSpacesAndDotDot(t *testing.T) {
	fixture := makeRelativeGitMetadataRepository(t)
	roots, err := ValidateRoots(validEnvelope(fixture.repository, filepath.Join(t.TempDir(), "state")))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if roots.GitDirectory != fixture.gitDirectory || roots.GitCommonDirectory != fixture.commonDirectory {
		t.Fatalf("Git directories = %q, %q, want %q, %q", roots.GitDirectory, roots.GitCommonDirectory, fixture.gitDirectory, fixture.commonDirectory)
	}
}

func TestGitDiscoveryFailuresDoNotLeakCapturedCapabilities(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("requires /proc/self/fd")
	}
	repository := makeRepository(t)
	if err := os.WriteFile(filepath.Join(repository, ".git", "commondir"), []byte("missing-common-directory\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	before := openFDCount(t)
	for range 20 {
		if _, err := ValidateRoots(validEnvelope(repository, filepath.Join(t.TempDir(), "state"))); !errors.Is(err, ErrUnsafeRoot) {
			t.Fatalf("Git discovery error = %v, want ErrUnsafeRoot", err)
		}
	}
	if after := openFDCount(t); after != before {
		t.Fatalf("Git discovery capability leak: before=%d after=%d", before, after)
	}
}

func TestPostDiscoveryValidationFailuresDoNotLeakCapturedCapabilities(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("requires /proc/self/fd")
	}
	repository := makeRepository(t)
	unsafeState := filepath.Join(t.TempDir(), "unsafe-state")
	if err := os.Mkdir(unsafeState, 0o755); err != nil {
		t.Fatal(err)
	}
	symlinkedState := filepath.Join(t.TempDir(), "symlinked-state")
	mustSymlink(t, t.TempDir(), symlinkedState)
	for name, state := range map[string]string{
		"state-capture": symlinkedState,
		"overlap":       repository,
		"state-owner":   unsafeState,
	} {
		t.Run(name, func(t *testing.T) {
			before := openFDCount(t)
			for range 20 {
				if _, err := ValidateRoots(validEnvelope(repository, state)); err == nil {
					t.Fatal("ValidateRoots unexpectedly accepted hostile state")
				}
			}
			if after := openFDCount(t); after != before {
				t.Fatalf("post-discovery capability leak: before=%d after=%d", before, after)
			}
		})
	}
}

func TestRootsCloseReleasesTransferredGitCapabilities(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("requires /proc/self/fd")
	}
	repository := makeRepository(t)
	before := openFDCount(t)
	roots, err := ValidateRoots(validEnvelope(repository, filepath.Join(t.TempDir(), "state")))
	if err != nil {
		t.Fatal(err)
	}
	if during := openFDCount(t); during <= before {
		t.Fatalf("ValidateRoots retained no capabilities: before=%d during=%d", before, during)
	}
	if err := roots.Close(); err != nil {
		t.Fatal(err)
	}
	if err := roots.Close(); err != nil {
		t.Fatalf("second Close error = %v", err)
	}
	if after := openFDCount(t); after != before {
		t.Fatalf("Roots.Close capability leak: before=%d after=%d", before, after)
	}
}

func TestStateFileOperationsRejectHardlinksAndNeverTruncateBeforeValidation(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("hardlink ownership is covered by Windows ACL validation")
	}
	repo := makeRepository(t)
	repositoryFile := filepath.Join(repo, "source.go")
	if err := os.WriteFile(repositoryFile, []byte("repository bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(repositoryFile, filepath.Join(state, "linked")); err != nil {
		t.Fatal(err)
	}
	roots, err := ValidateRoots(validEnvelope(repo, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if _, err := roots.OpenStateFile("linked"); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("hardlink open error = %v, want ErrUnsafeRoot", err)
	}
	if _, err := roots.CreateStateFile("linked"); err == nil {
		t.Fatal("CreateStateFile accepted existing hardlink")
	}
	if got, err := os.ReadFile(repositoryFile); err != nil || string(got) != "repository bytes" {
		t.Fatalf("repository bytes changed: %q, %v", got, err)
	}
	if err := os.WriteFile(filepath.Join(state, "insecure"), []byte("must survive"), 0o666); err != nil {
		t.Fatal(err)
	}
	if _, err := roots.OpenStateFile("insecure"); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("insecure file error = %v, want ErrUnsafeRoot", err)
	}
	if got, err := os.ReadFile(filepath.Join(state, "insecure")); err != nil || string(got) != "must survive" {
		t.Fatalf("insecure file was mutated before validation: %q, %v", got, err)
	}
}

func TestReplaceStateFileValidatesThenAtomicallyReplaces(t *testing.T) {
	repo := makeRepository(t)
	state := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(state, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(state, "current"), []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	roots, err := ValidateRoots(validEnvelope(repo, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if err := roots.ReplaceStateFile("current", []byte("new")); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(filepath.Join(state, "current")); err != nil || string(got) != "new" {
		t.Fatalf("replacement = %q, %v", got, err)
	}
}

func TestEnsureStateHostileFailuresDoNotLeakRootHandles(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("requires /proc/self/fd")
	}
	repo := makeRepository(t)
	base := t.TempDir()
	roots, err := ValidateRoots(validEnvelope(repo, filepath.Join(base, "first", "second")))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	stateCreateHook = func(component string) {
		if component == "second" {
			mustSymlink(t, t.TempDir(), filepath.Join(base, "first", "second"))
		}
	}
	t.Cleanup(func() { stateCreateHook = nil })
	before := openFDCount(t)
	for range 20 {
		if err := roots.EnsureState(); !errors.Is(err, ErrUnsafeRoot) {
			t.Fatalf("EnsureState error = %v", err)
		}
	}
	if after := openFDCount(t); after != before {
		t.Fatalf("file descriptor leak: before=%d after=%d", before, after)
	}
}

func TestEnsureStateRejectsSymlinkInsertedDuringCreation(t *testing.T) {
	repo := makeRepository(t)
	base := t.TempDir()
	state := filepath.Join(base, "new-state", "nested")
	roots, err := ValidateRoots(validEnvelope(repo, state))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	outside := t.TempDir()
	stateCreateHook = func(component string) {
		if component == "new-state" {
			mustSymlink(t, outside, filepath.Join(base, component))
		}
	}
	t.Cleanup(func() { stateCreateHook = nil })
	if err := roots.EnsureState(); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("EnsureState error = %v, want ErrUnsafeRoot", err)
	}
	if _, err := os.Stat(filepath.Join(outside, "nested")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("state creation escaped through symlink: %v", err)
	}
}

func TestValidateRootsParsesGitMetadataWithoutEnvironmentOrChildProcess(t *testing.T) {
	repo := makeRepository(t)
	gitDirectory := filepath.Join(repo, ".git")
	separate := filepath.Join(repo, "control dir")
	if err := os.Rename(gitDirectory, separate); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(gitDirectory, []byte("gitdir: control dir\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(separate, 0o750); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GIT_DIR", t.TempDir())
	roots, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state")))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if roots.GitDirectory != separate || roots.GitCommonDirectory != separate {
		t.Fatalf("Git directories = %q, %q", roots.GitDirectory, roots.GitCommonDirectory)
	}
	if _, err := roots.OpenRepositoryFile("control dir/HEAD", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("opened discovered Git directory: %v", err)
	}
	if _, err := roots.OpenRepositoryFile(".GIT/HEAD", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("opened case-insensitive Git alias: %v", err)
	}
	caseAlias := filepath.Join(repo, "case-alias")
	if err := os.Mkdir(caseAlias, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(caseAlias, "HEAD"), []byte("outside-git"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(caseAlias, "refs"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(caseAlias, "refs"), 0o711); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(caseAlias, "refs", "branch"), []byte("outside-git"), 0o600); err != nil {
		t.Fatal(err)
	}
	// This models a case-insensitive filesystem reporting the alias as the
	// captured separate Git directory. It keeps the check deterministic on
	// case-sensitive CI filesystems.
	identityEqualHook = func(first, second os.FileInfo) bool {
		if os.SameFile(first, second) {
			return true
		}
		return first.IsDir() && second.IsDir() && ((first.Mode().Perm() == 0o700 && second.Mode().Perm() == 0o750) || (first.Mode().Perm() == 0o750 && second.Mode().Perm() == 0o700))
	}
	t.Cleanup(func() { identityEqualHook = nil })
	if _, err := roots.OpenRepositoryFile("case-alias/HEAD", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("opened identity-aliased Git directory: %v", err)
	}
	if _, err := roots.OpenRepositoryFile("case-alias/refs/branch", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("opened descendant of identity-aliased Git directory: %v", err)
	}
	if _, err := ValidateRoots(validEnvelope(repo, filepath.Join(separate, "state"))); !errors.Is(err, ErrRootOverlap) {
		t.Fatalf("state inside discovered Git directory error = %v", err)
	}
}

func TestValidateRootsRejectsMalformedGitMetadata(t *testing.T) {
	repo := makeRepository(t)
	gitFile := filepath.Join(repo, ".git")
	if err := os.RemoveAll(gitFile); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(gitFile, append([]byte("gitdir: "), bytes.Repeat([]byte("x"), 8192)...), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state"))); !errors.Is(err, ErrUnsafeRoot) {
		t.Fatalf("malformed Git metadata error = %v", err)
	}
}

func TestValidateRootsRejectsStateInsideLinkedWorktreeGitDirectories(t *testing.T) {
	primary := makeRepository(t)
	worktree := filepath.Join(t.TempDir(), "linked")
	git(t, primary, "worktree", "add", "--detach", worktree, "HEAD")

	gitDirectory := gitOutput(t, worktree, "rev-parse", "--path-format=absolute", "--git-dir")
	commonDirectory := gitOutput(t, worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
	if gitDirectory == commonDirectory {
		t.Fatalf("linked worktree git dir = common dir = %q", gitDirectory)
	}
	for _, state := range []string{
		filepath.Join(gitDirectory, "taf-state"),
		filepath.Join(commonDirectory, "taf-state"),
	} {
		_, err := ValidateRoots(validEnvelope(worktree, state))
		if !errors.Is(err, ErrRootOverlap) {
			t.Fatalf("state %q error = %v, want ErrRootOverlap", state, err)
		}
	}
}

func TestOpenRepositoryFileRejectsHostilePaths(t *testing.T) {
	roots := makeRoots(t)
	for _, relative := range []string{"", "/etc/passwd", "../outside.go", "dir/../outside.go", "dir\\outside.go", "C:/outside.go", ".git/config"} {
		_, err := roots.OpenRepositoryFile(relative, 1024)
		if !errors.Is(err, ErrUnsafePath) {
			t.Fatalf("OpenRepositoryFile(%q) error = %v, want ErrUnsafePath", relative, err)
		}
	}
}

func TestOpenRepositoryFileRejectsSymlinksAndSpecialFiles(t *testing.T) {
	roots := makeRoots(t)
	mustSymlink(t, outsideFile(t), filepath.Join(roots.Repository, "escape.go"))
	if _, err := roots.OpenRepositoryFile("escape.go", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("symlink error = %v, want ErrUnsafePath", err)
	}
	if runtime.GOOS == "linux" {
		device := filepath.Join(roots.Repository, "null.device")
		if output, err := exec.Command("mknod", device, "c", "1", "3").CombinedOutput(); err == nil {
			if _, err := roots.OpenRepositoryFile("null.device", 1024); !errors.Is(err, ErrUnsafePath) {
				t.Fatalf("device error = %v, want ErrUnsafePath", err)
			}
		} else {
			t.Logf("cannot create device node: %v: %s", err, output)
		}
	}

	fifo := filepath.Join(roots.Repository, "events.fifo")
	if err := exec.Command("mkfifo", fifo).Run(); err == nil {
		if _, err := roots.OpenRepositoryFile("events.fifo", 1024); !errors.Is(err, ErrUnsafePath) {
			t.Fatalf("FIFO error = %v, want ErrUnsafePath", err)
		}
	} else {
		t.Logf("cannot create FIFO: %v", err)
	}

	socket := filepath.Join(roots.Repository, "service.sock")
	listener, err := net.Listen("unix", socket)
	if err == nil {
		defer listener.Close()
		if _, err := roots.OpenRepositoryFile("service.sock", 1024); !errors.Is(err, ErrUnsafePath) {
			t.Fatalf("socket error = %v, want ErrUnsafePath", err)
		}
	}
}

func TestOpenRepositoryFileRejectsReplacementDuringRead(t *testing.T) {
	roots := makeRoots(t)
	path := filepath.Join(roots.Repository, "changing.go")
	if err := os.WriteFile(path, []byte("trusted bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	candidate := filepath.Join(roots.Repository, "attacker.go")
	if err := os.WriteFile(candidate, []byte("attacker bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	repositoryOpenHook = func() {
		if err := os.Rename(candidate, path); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { repositoryOpenHook = nil })
	file, err := roots.OpenRepositoryFile("changing.go", 1024)
	if !errors.Is(err, ErrUnstableFile) {
		t.Fatalf("replacement read error = %v, want ErrUnstableFile", err)
	}
	if file.Bytes != nil || file.SHA256 != "" {
		t.Fatalf("hostile replacement returned bytes: %#v", file)
	}
}

func TestOpenRepositoryFileRejectsOversizedFile(t *testing.T) {
	roots := makeRoots(t)
	if err := os.WriteFile(filepath.Join(roots.Repository, "large.go"), []byte("012345"), 0o600); err != nil {
		t.Fatal(err)
	}
	file, err := roots.OpenRepositoryFile("large.go", 5)
	if !errors.Is(err, ErrFileTooLarge) {
		t.Fatalf("error = %v, want ErrFileTooLarge", err)
	}
	if file.Bytes != nil {
		t.Fatalf("oversized read returned bytes: %q", file.Bytes)
	}
}

func TestOpenRepositoryFileReturnsStableRegularFile(t *testing.T) {
	roots := makeRoots(t)
	contents := []byte("package example\n")
	if err := os.WriteFile(filepath.Join(roots.Repository, "example.go"), contents, 0o600); err != nil {
		t.Fatal(err)
	}
	file, err := roots.OpenRepositoryFile("example.go", 1024)
	if err != nil {
		t.Fatal(err)
	}
	wantDigest := sha256.Sum256(contents)
	if file.RelativePath != "example.go" || string(file.Bytes) != string(contents) || file.Size != int64(len(contents)) || file.SHA256 != fmtDigest(wantDigest) {
		t.Fatalf("file = %#v, want stable regular file", file)
	}
}

func TestOpenRepositoryFileRejectsGitMetadata(t *testing.T) {
	roots := makeRoots(t)
	if _, err := roots.OpenRepositoryFile(".git/HEAD", 1024); !errors.Is(err, ErrUnsafePath) {
		t.Fatalf("metadata read error = %v, want ErrUnsafePath", err)
	}
}

func makeRoots(t *testing.T) Roots {
	t.Helper()
	repo := makeRepository(t)
	roots, err := ValidateRoots(validEnvelope(repo, filepath.Join(t.TempDir(), "state")))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = roots.Close() })
	return roots
}

func makeRepository(t *testing.T) string {
	t.Helper()
	repo := filepath.Join(t.TempDir(), "repository")
	if err := os.Mkdir(repo, 0o700); err != nil {
		t.Fatal(err)
	}
	git(t, repo, "init", "-q")
	if err := os.WriteFile(filepath.Join(repo, "README.md"), []byte("fixture\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(t, repo, "add", "README.md")
	git(t, repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
	return repo
}

func validEnvelope(repository, state string) wire.Envelope {
	return wire.Envelope{RepositoryRoot: repository, StateRoot: state}
}

func outsideFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "outside.go")
	if err := os.WriteFile(path, []byte("outside"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func mustSymlink(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("cannot create symlink: %v", err)
	}
}

func git(t *testing.T, directory string, args ...string) {
	t.Helper()
	if output, err := exec.Command("git", append([]string{"-C", directory}, args...)...).CombinedOutput(); err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, output)
	}
}

func gitOutput(t *testing.T, directory string, args ...string) string {
	t.Helper()
	output, err := exec.Command("git", append([]string{"-C", directory}, args...)...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, output)
	}
	return strings.TrimSpace(string(output))
}

func fmtDigest(sum [sha256.Size]byte) string {
	const hex = "0123456789abcdef"
	encoded := make([]byte, len(sum)*2)
	for i, value := range sum {
		encoded[i*2] = hex[value>>4]
		encoded[i*2+1] = hex[value&0x0f]
	}
	return string(encoded)
}

func openFDCount(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	return len(entries)
}

func openFDCountIfSupported(t *testing.T) int {
	t.Helper()
	if runtime.GOOS != "linux" {
		return -1
	}
	return openFDCount(t)
}

func assertOpenFDCountUnchanged(t *testing.T, before int) {
	t.Helper()
	if before < 0 {
		return
	}
	if after := openFDCount(t); after != before {
		t.Fatalf("metadata discovery capability leak: before=%d after=%d", before, after)
	}
}

type relativeGitMetadataFixture struct {
	base            string
	repository      string
	controlBase     string
	gitDirectory    string
	commonDirectory string
}

func makeRelativeGitMetadataRepository(t *testing.T) relativeGitMetadataFixture {
	t.Helper()
	container := t.TempDir()
	base := filepath.Join(container, "workspace base")
	repository := filepath.Join(base, "repository with spaces")
	if err := os.MkdirAll(repository, 0o700); err != nil {
		t.Fatal(err)
	}
	git(t, repository, "init", "-q")
	if err := os.WriteFile(filepath.Join(repository, "README.md"), []byte("fixture\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(t, repository, "add", "README.md")
	git(t, repository, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")

	controlBase := filepath.Join(base, "control metadata with spaces")
	commonDirectory := filepath.Join(controlBase, "common metadata")
	if err := os.MkdirAll(controlBase, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(repository, ".git"), commonDirectory); err != nil {
		t.Fatal(err)
	}
	gitDirectory := filepath.Join(controlBase, "worktrees", "git dir")
	if err := os.MkdirAll(gitDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	head, err := os.ReadFile(filepath.Join(commonDirectory, "HEAD"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitDirectory, "HEAD"), head, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitDirectory, "commondir"), []byte("../../common metadata\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, ".git"), []byte("gitdir: ../control metadata with spaces/worktrees/git dir\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return relativeGitMetadataFixture{
		base:            base,
		repository:      repository,
		controlBase:     controlBase,
		gitDirectory:    gitDirectory,
		commonDirectory: commonDirectory,
	}
}

type directoryTreeEntry struct {
	Mode     os.FileMode
	Contents string
}

func snapshotDirectoryTree(t *testing.T, root string) map[string]directoryTreeEntry {
	t.Helper()
	snapshot := make(map[string]directoryTreeEntry)
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		value := directoryTreeEntry{Mode: info.Mode()}
		if info.Mode().IsRegular() {
			contents, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			value.Contents = string(contents)
		} else if info.Mode()&os.ModeSymlink != 0 {
			target, err := os.Readlink(path)
			if err != nil {
				return err
			}
			value.Contents = target
		}
		snapshot[filepath.ToSlash(relative)] = value
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}
