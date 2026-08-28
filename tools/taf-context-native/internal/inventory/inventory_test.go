package inventory

import (
	"bytes"
	"crypto/sha1"
	"crypto/sha256"
	endian "encoding/binary"
	"encoding/json"
	"fmt"
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
	assertExclusion(t, result, "ignored/code.py", ExcludedIgnored)
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
	if result.FullBodyOpens != 0 || result.PrefixBytes > 2*uint64(binaryPrefixBytes) {
		t.Fatalf("estimate read complete eligible bodies or exceeded prefixes: %#v", result)
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

func TestCollectAppliesNestedGitIgnoreRulesOnlyWithinTheirDirectory(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "a/.gitignore", "*.go\n")
	writeFile(t, repository, "a/one.go", "package a\n")
	writeFile(t, repository, "b/two.go", "package b\n")

	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "b/two.go", Language: "go", Size: 10}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("paths = %#v, want only b/two.go", got)
	}
	assertExclusion(t, result, "a/one.go", ExcludedIgnored)
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

func TestCollectHonorsGitIndexOverIgnoreRules(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "ignored.go\n")
	writeFile(t, repository, "ignored.go", "package ignored\n")
	writeGitIndex(t, repository, []string{"ignored.go"})
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "ignored.go", Language: "go", Size: 16}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("paths = %#v, want tracked ignored file included", got)
	}
}

func TestCollectUsesLinkedWorktreeGitIndex(t *testing.T) {
	repository := newRepository(t)
	if err := os.Remove(filepath.Join(repository, ".git")); err != nil {
		t.Fatal(err)
	}
	gitDirectory := filepath.Join(t.TempDir(), "worktree-git")
	if err := os.Mkdir(gitDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, repository, ".git", "gitdir: "+gitDirectory+"\n")
	writeFile(t, repository, ".gitignore", "tracked.go\n")
	writeFile(t, repository, "tracked.go", "package tracked\n")
	writeGitIndexIn(t, gitDirectory, []string{"tracked.go"})
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "tracked.go", Language: "go", Size: 16}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("linked-worktree tracked result = %#v", result)
	}
}

func TestCollectMarksMalformedGitIndexPartial(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "source.go", "package source\n")
	if err := os.WriteFile(filepath.Join(repository, ".git", "index"), []byte("DIRCbroken"), 0o600); err != nil {
		t.Fatal(err)
	}
	result := mustCollect(t, repository, ModeBuild)
	if !result.Partial || !contains(result.Warnings, "git-index-invalid") {
		t.Fatalf("malformed index result = %#v", result)
	}
}

func TestBinaryPrefixAllowsOnlyTrailingPartialRune(t *testing.T) {
	validSplit := append(bytes.Repeat([]byte("a"), 8191), 0xe2)
	if binary(validSplit, true) {
		t.Fatal("truncated final UTF-8 rune classified as binary")
	}
	invalidInterior := append([]byte{0xff}, bytes.Repeat([]byte("a"), 8191)...)
	if !binary(invalidInterior, true) || !binary([]byte("ok\x00no"), false) {
		t.Fatal("invalid UTF-8 or NUL content not classified as binary")
	}
}

func TestExtensionRegistryIncludesBoundedConfigFormats(t *testing.T) {
	registry := ExtensionRegistry()
	seen := map[string]bool{}
	for _, metadata := range registry {
		seen[metadata.Language] = true
	}
	for _, language := range []string{"json", "toml", "yaml", "ini"} {
		if !seen[language] {
			t.Fatalf("registry missing %s: %#v", language, registry)
		}
	}
	for _, relative := range []string{"config.json", "config.toml", "config.yaml", "config.ini"} {
		if languageForPath(relative) == "" {
			t.Fatalf("registry/inventory mismatch for %s", relative)
		}
	}
}

func TestCollectBuildSHA256MatchesBytes(t *testing.T) {
	repository := newRepository(t)
	contents := "package digest\n"
	writeFile(t, repository, "digest.go", contents)
	result := mustCollect(t, repository, ModeBuild)
	digest := sha256.Sum256([]byte(contents))
	if got, want := result.Paths[0].SHA256, fmt.Sprintf("%x", digest); got != want {
		t.Fatalf("SHA256 = %s, want %s", got, want)
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
	if result.Coverage.ExclusionReasonCounts[ExcludedLimit] != 0 || !result.UnknownRemainder || result.Coverage.PathCoverage != 0 {
		t.Fatalf("path-limit result must represent an unknown conservative remainder: %#v", result)
	}
	if result.DirectoryEntries != 4 || result.PrefixBytes != uint64(2*len("package a\n")) || result.FullBodyOpens != 2 || len(result.Paths) > limits.MaximumEligiblePaths {
		t.Fatalf("path ceiling performed unbounded work: %#v", result)
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

func writeGitIndex(t *testing.T, repository string, names []string) {
	t.Helper()
	writeGitIndexIn(t, filepath.Join(repository, ".git"), names)
}

func writeGitIndexIn(t *testing.T, gitDirectory string, names []string) {
	t.Helper()
	var raw bytes.Buffer
	raw.WriteString("DIRC")
	_ = endian.Write(&raw, endian.BigEndian, uint32(2))
	_ = endian.Write(&raw, endian.BigEndian, uint32(len(names)))
	for _, name := range names {
		start := raw.Len()
		raw.Write(make([]byte, 60))
		_ = endian.Write(&raw, endian.BigEndian, uint16(len(name)))
		raw.WriteString(name)
		raw.WriteByte(0)
		for (raw.Len()-start)%8 != 0 {
			raw.WriteByte(0)
		}
	}
	digest := sha1.Sum(raw.Bytes())
	raw.Write(digest[:])
	if err := os.WriteFile(filepath.Join(gitDirectory, "index"), raw.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
}
