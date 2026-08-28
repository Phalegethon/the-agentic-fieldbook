package inventory

import (
	"bytes"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestCollectIsIndependentOfCreationOrder(t *testing.T) {
	left := inventoryFixture(t, []string{"b.go", "docs/a.md", "a.py"})
	right := inventoryFixture(t, []string{"a.py", "b.go", "docs/a.md"})
	leftResult := mustCollect(t, left, ModeBuild)
	rightResult := mustCollect(t, right, ModeBuild)
	if diff := cmpInventory(leftResult, rightResult); diff != "" {
		t.Fatal(diff)
	}
}

func TestCollectClassifiesAndBoundsFilesDeterministically(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "ignored/\n")
	writeFile(t, repository, "source.go", "package example\n")
	writeFile(t, repository, "document.md", "# heading\n")
	writeFile(t, repository, "binary.go", "ok\x00not text")
	writeFile(t, repository, "nonutf8.go", string([]byte{0xff, 0xfe}))
	writeFile(t, repository, "unknown.xyz", "unrecognized")
	writeFile(t, repository, "generated/code.go", "package generated\n")
	writeFile(t, repository, "vendor/code.go", "package vendor\n")
	writeFile(t, repository, "node_modules/code.js", "module.exports = 1\n")
	writeFile(t, repository, "target/code.rs", "fn main() {}\n")
	writeFile(t, repository, "ignored/code.py", "pass\n")
	writeFile(t, repository, "maximum.go", strings.Repeat("a", policy.ProductionLimits().MaximumSourceFileBytes))
	writeFile(t, repository, "too-large.go", strings.Repeat("a", policy.ProductionLimits().MaximumSourceFileBytes+1))
	writeFile(t, repository, "maximum.md", strings.Repeat("a", policy.ProductionLimits().MaximumMarkdownFileBytes))
	writeFile(t, repository, "too-large.md", strings.Repeat("a", policy.ProductionLimits().MaximumMarkdownFileBytes+1))
	if err := os.Symlink(filepath.Join(repository, "source.go"), filepath.Join(repository, "linked.go")); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		if err := os.Mkdir(filepath.Join(repository, "special"), 0o700); err != nil {
			t.Fatal(err)
		}
		listener, err := net.Listen("unix", filepath.Join(repository, "special.sock"))
		if err != nil {
			t.Fatal(err)
		}
		defer listener.Close()
	}

	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{
		{RelativePath: "document.md", Language: "markdown", Size: 10},
		{RelativePath: "maximum.go", Language: "go", Size: int64(policy.ProductionLimits().MaximumSourceFileBytes)},
		{RelativePath: "maximum.md", Language: "markdown", Size: int64(policy.ProductionLimits().MaximumMarkdownFileBytes)},
		{RelativePath: "source.go", Language: "go", Size: 16},
	}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("paths = %#v, want %#v", got, want)
	}
	assertExclusion(t, result, "binary.go", ExcludedBinary)
	assertExclusion(t, result, "nonutf8.go", ExcludedBinary)
	assertExclusion(t, result, "unknown.xyz", ExcludedUnsupported)
	assertExclusion(t, result, "generated", ExcludedGenerated)
	assertExclusion(t, result, "vendor", ExcludedVendored)
	assertExclusion(t, result, "node_modules", ExcludedVendored)
	assertExclusion(t, result, "target", ExcludedGenerated)
	assertExclusion(t, result, "ignored", ExcludedIgnored)
	assertExclusion(t, result, "too-large.go", ExcludedOversized)
	assertExclusion(t, result, "too-large.md", ExcludedOversized)
	assertExclusion(t, result, "linked.go", ExcludedUnsafe)
	if runtime.GOOS != "windows" {
		assertExclusion(t, result, "special.sock", ExcludedUnsafe)
	}
	if !sort.SliceIsSorted(result.Paths, func(i, j int) bool { return result.Paths[i].RelativePath < result.Paths[j].RelativePath }) {
		t.Fatalf("paths are not sorted: %#v", result.Paths)
	}
	if !sort.SliceIsSorted(result.Exclusions, func(i, j int) bool { return result.Exclusions[i].RelativePath < result.Exclusions[j].RelativePath }) {
		t.Fatalf("exclusions are not sorted: %#v", result.Exclusions)
	}
	first := mustCollect(t, repository, ModeBuild)
	second := mustCollect(t, repository, ModeBuild)
	if diff := cmpInventory(first, second); diff != "" {
		t.Fatalf("repeated collection differs: %s", diff)
	}
}

func TestCollectEstimateUsesOnlyBoundedPrefixesAndIsPartial(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "source.go", strings.Repeat("x", 128*1024))
	writeFile(t, repository, "binary.py", "\x00"+strings.Repeat("x", 128*1024))

	result := mustCollect(t, repository, ModeEstimate)
	if !result.Partial || !contains(result.Warnings, "coverage-estimated-not-parsed") {
		t.Fatalf("estimate result = %#v, want partial estimated coverage warning", result)
	}
	if result.Coverage.ParseFailureCount != 0 {
		t.Fatalf("estimate parse failures = %d, want 0", result.Coverage.ParseFailureCount)
	}
	if result.Paths[0].SHA256 != "" {
		t.Fatalf("estimate calculated a full-body digest: %#v", result.Paths[0])
	}
	assertExclusion(t, result, "binary.py", ExcludedBinary)
}

func TestCollectExcludesLinkedAndNestedGitMetadata(t *testing.T) {
	repository := newRepository(t)
	if err := os.Remove(filepath.Join(repository, ".git")); err != nil {
		t.Fatal(err)
	}
	gitDirectory := filepath.Join(t.TempDir(), "linked-worktree-git")
	if err := os.Mkdir(gitDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, repository, ".git", "gitdir: "+gitDirectory+"\n")
	writeFile(t, repository, "nested/.git/HEAD", "ref: refs/heads/main\n")
	writeFile(t, repository, "nested/source.py", "print('safe')\n")

	result := mustCollect(t, repository, ModeBuild)
	assertExclusion(t, result, ".git", ExcludedGit)
	assertExclusion(t, result, "nested/.git", ExcludedGit)
	for _, candidate := range result.Paths {
		if strings.Contains(candidate.RelativePath, ".git/") {
			t.Fatalf("Git metadata was inventoried: %#v", result.Paths)
		}
	}
}

func TestCollectDoesNotApplyNestedGitIgnoreRulesRepositoryWide(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "a/.gitignore", "*.go\n")
	writeFile(t, repository, "a/one.go", "package a\n")
	writeFile(t, repository, "b/two.go", "package b\n")

	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{
		{RelativePath: "a/one.go", Language: "go", Size: 10},
		{RelativePath: "b/two.go", Language: "go", Size: 10},
	}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("paths = %#v, want nested ignore not to leak", got)
	}
}

func TestCollectEstimateDoesNotCreateStateOrMutateRepository(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "source.go", strings.Repeat("x", 32*1024))
	state := filepath.Join(t.TempDir(), "state")
	before := repositorySnapshot(t, repository)
	roots, err := boundary.ValidateRoots(wire.Envelope{RepositoryRoot: repository, StateRoot: state})
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	if _, err := Collect(roots, ModeEstimate); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(state); !os.IsNotExist(err) {
		t.Fatalf("estimate created state: %v", err)
	}
	if after := repositorySnapshot(t, repository); !reflect.DeepEqual(before, after) {
		t.Fatalf("estimate mutated repository: before=%#v after=%#v", before, after)
	}
}

func TestCollectStopsAtPathAndByteLimitsWithoutClaimingCompleteness(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "a.go", "package a\n")
	writeFile(t, repository, "b.go", "package b\n")
	writeFile(t, repository, "c.go", "package c\n")
	writeFile(t, repository, "d.go", "package d\n")
	writeFile(t, repository, "e.go", "package e\n")
	limits := policy.ProductionLimits()
	limits.MaximumEligiblePaths = 2
	roots := validatedRoots(t, repository)
	defer roots.Close()
	result, err := collect(roots, ModeBuild, limits)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Partial || !contains(result.Warnings, "inventory-path-limit") || len(result.Paths) != 2 {
		t.Fatalf("path-limited result = %#v", result)
	}
	if got := result.Coverage.ExclusionReasonCounts[ExcludedLimit]; got != 3 {
		t.Fatalf("path-limit exclusions = %d, want all 3 omitted source paths", got)
	}
	if got, want := result.Coverage.PathCoverage, 2.0/6.0; got != want {
		t.Fatalf("path coverage = %v, want %v", got, want)
	}

	limits.MaximumEligiblePaths = 10
	limits.MaximumEligibleSourceBytes = uint64(len("package a\n"))
	result, err = collect(roots, ModeBuild, limits)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Partial || !contains(result.Warnings, "inventory-byte-limit") || len(result.Paths) != 1 {
		t.Fatalf("byte-limited result = %#v", result)
	}
}

func inventoryFixture(t *testing.T, names []string) string {
	t.Helper()
	repository := newRepository(t)
	for _, name := range names {
		contents := "package example\n"
		if strings.HasSuffix(name, ".py") {
			contents = "print('example')\n"
		}
		if strings.HasSuffix(name, ".md") {
			contents = "# example\n"
		}
		writeFile(t, repository, name, contents)
	}
	return repository
}

func mustCollect(t *testing.T, repository string, mode Mode) Result {
	t.Helper()
	roots := validatedRoots(t, repository)
	defer roots.Close()
	result, err := Collect(roots, mode)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func validatedRoots(t *testing.T, repository string) boundary.Roots {
	t.Helper()
	roots, err := boundary.ValidateRoots(wire.Envelope{RepositoryRoot: repository, StateRoot: filepath.Join(t.TempDir(), "state")})
	if err != nil {
		t.Fatal(err)
	}
	return roots
}

func newRepository(t *testing.T) string {
	t.Helper()
	repository := t.TempDir()
	if err := os.Mkdir(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	return repository
}

func writeFile(t *testing.T, root, relative, contents string) {
	t.Helper()
	path := filepath.Join(root, filepath.FromSlash(relative))
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}

func assertExclusion(t *testing.T, result Result, relative, reason string) {
	t.Helper()
	for _, exclusion := range result.Exclusions {
		if exclusion.RelativePath == relative && exclusion.Reason == reason {
			return
		}
	}
	t.Fatalf("exclusion %q/%q missing from %#v", relative, reason, result.Exclusions)
}

func cmpInventory(left, right Result) string {
	leftBytes, leftErr := json.Marshal(left)
	rightBytes, rightErr := json.Marshal(right)
	if leftErr != nil || rightErr != nil {
		return "failed to marshal result"
	}
	if !bytes.Equal(leftBytes, rightBytes) {
		return string(leftBytes) + " != " + string(rightBytes)
	}
	return ""
}

func samePathsIgnoringDigest(got, want []Path) bool {
	if len(got) != len(want) {
		return false
	}
	for index := range got {
		if got[index].RelativePath != want[index].RelativePath || got[index].Language != want[index].Language || got[index].Size != want[index].Size || got[index].SHA256 == "" {
			return false
		}
	}
	return true
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func repositorySnapshot(t *testing.T, repository string) map[string]string {
	t.Helper()
	snapshot := map[string]string{}
	err := filepath.WalkDir(repository, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(repository, path)
		if err != nil {
			return err
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		snapshot[filepath.ToSlash(relative)] = string(contents)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}
