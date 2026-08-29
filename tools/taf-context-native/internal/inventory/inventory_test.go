package inventory

import (
	"bytes"
	"crypto/sha1"
	"crypto/sha256"
	endian "encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
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

func TestCollectLoadsOnlyExactGitIgnoreBasenameBeforeChildren(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".a.go", "package a\n")
	writeFile(t, repository, "foo.gitignore", "*.go\n")
	writeFile(t, repository, ".gitignore", "*.go\n!kept.go\n")
	writeFile(t, repository, "kept.go", "package kept\n")
	writeFile(t, repository, "ignored.go", "package ignored\n")
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "kept.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("exact .gitignore policy result = %#v", result)
	}
	assertExclusion(t, result, ".a.go", ExcludedIgnored)
	assertExclusion(t, result, "ignored.go", ExcludedIgnored)
}

func TestCollectLoadsCommonGitInfoExcludeBeforeWalk(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".git/info/exclude", "excluded.go\n")
	writeFile(t, repository, "excluded.go", "package excluded\n")
	writeFile(t, repository, "kept.go", "package kept\n")
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "kept.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("info/exclude result = %#v", result)
	}
	assertExclusion(t, result, "excluded.go", ExcludedIgnored)
}

func TestCollectCannotReincludeFileBelowExcludedParent(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "parent/\n!parent/keep.go\n")
	writeFile(t, repository, "parent/keep.go", "package keep\n")
	result := mustCollect(t, repository, ModeBuild)
	if len(result.Paths) != 0 {
		t.Fatalf("excluded-parent negation paths = %#v, want none", result.Paths)
	}
	assertExclusion(t, result, "parent", ExcludedIgnored)
}

func TestCollectPreservesTrackedDescendantOfIgnoredDirectory(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "parent/\n")
	writeFile(t, repository, "parent/tracked.go", "package tracked\n")
	writeFile(t, repository, "parent/untracked.go", "package untracked\n")
	writeGitIndex(t, repository, []string{"parent/tracked.go"})
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "parent/tracked.go", Language: "go", Size: 16}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("tracked descendant result = %#v", result)
	}
	assertExclusion(t, result, "parent/untracked.go", ExcludedIgnored)
}

func TestCollectParsesSignificantSpacesEscapesAndBracketRanges(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", strings.Join([]string{
		"plain.go   ",
		`trailing.go\ `,
		`literal\*.go`,
		`literal\[.go`,
		"[a-c].go",
		"[!a-c]anged.go",
		" leading.go",
	}, "\n")+"\n")
	for _, relative := range []string{"plain.go", "trailing.go ", "literal*.go", "literal[.go", "a.go", "b.go", "danged.go", " leading.go"} {
		writeFile(t, repository, relative, "package ignored\n")
	}
	writeFile(t, repository, "kept.go", "package kept\n")
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "kept.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("spaces/escapes/brackets result = %#v", result)
	}
	for _, relative := range []string{"plain.go", "trailing.go ", "literal*.go", "literal[.go", "a.go", "b.go", "danged.go", " leading.go"} {
		assertExclusion(t, result, relative, ExcludedIgnored)
	}
}

func TestCollectHonorsAnchoredSlashDirectoryAndOrderedNegationRules(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "/root.go\nnested/*.go\ncache/\n*.py\n!keep.py\n")
	writeFile(t, repository, "root.go", "package root\n")
	writeFile(t, repository, "nested/root.go", "package nested\n")
	writeFile(t, repository, "other/root.go", "package other\n")
	writeFile(t, repository, "cache/hidden.go", "package cache\n")
	writeFile(t, repository, "drop.py", "drop = 1\n")
	writeFile(t, repository, "keep.py", "keep = 1\n")
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "keep.py", Language: "python", Size: 9}, {RelativePath: "other/root.go", Language: "go", Size: 14}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("anchored/directory/negation result = %#v", result)
	}
	assertExclusion(t, result, "root.go", ExcludedIgnored)
	assertExclusion(t, result, "nested/root.go", ExcludedIgnored)
	assertExclusion(t, result, "cache", ExcludedIgnored)
	assertExclusion(t, result, "drop.py", ExcludedIgnored)
}

func TestParseIgnoreRulesSkipsEmptyAndSlashOnlyPatternsWithoutPanicking(t *testing.T) {
	for _, pattern := range []string{"/", "!/", `\/`, "!", `\`, "//"} {
		t.Run(fmt.Sprintf("%q", pattern), func(t *testing.T) {
			rules, _, limited := parseIgnoreRules("", []byte(pattern+"\n"), maximumIgnoreRules, maximumIgnorePatternBytes)
			if limited {
				t.Fatalf("pattern %q unexpectedly exhausted policy budget", pattern)
			}
			for _, rule := range rules {
				if rule.pattern == "" || rule.matcher == nil {
					t.Fatalf("pattern %q produced no-op rule %#v", pattern, rule)
				}
			}
		})
	}
}

func FuzzParseIgnoreRulesNeverPanics(f *testing.F) {
	for _, seed := range [][]byte{[]byte("/"), []byte("!/"), []byte(`\/`), []byte("!"), []byte(`\`), []byte("["), {0xff, '/', '\n'}} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, contents []byte) {
		_, _, _ = parseIgnoreRules("", contents, maximumIgnoreRules, maximumIgnorePatternBytes)
	})
}

func TestCollectAnchoredDirectoryPatternDoesNotMatchNestedBasename(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "/foo/\n")
	writeFile(t, repository, "foo/ignored.go", "package ignored\n")
	writeFile(t, repository, "nested/foo/kept.go", "package kept\n")

	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "nested/foo/kept.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("anchored directory result = %#v, want %#v", result, want)
	}
	assertExclusion(t, result, "foo", ExcludedIgnored)
}

func TestCollectMatchesUnicodeIgnoreLiteralsAndRanges(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "café.go\ncaf[é-ê].py\n")
	writeFile(t, repository, "café.go", "package ignored\n")
	writeFile(t, repository, "cafê.py", "ignored = 1\n")
	writeFile(t, repository, "cafe.go", "package kept\n")

	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "cafe.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("Unicode ignore result = %#v, want %#v", result, want)
	}
	assertExclusion(t, result, "café.go", ExcludedIgnored)
	assertExclusion(t, result, "cafê.py", ExcludedIgnored)
}

func TestCollectOnlyTreatsDocumentedDoubleStarsAsSlashCrossing(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, ".gitignore", "foo/***/bar.go\nleading***/deep.py\nspecial/**/drop.rs\n**/everywhere.ts\ntail/**\n")
	for _, relative := range []string{
		"foo/one/bar.go",
		"foo/one/two/bar.go",
		"leading-one/deep.py",
		"leading-one/two/deep.py",
		"special/drop.rs",
		"special/one/two/drop.rs",
		"nested/everywhere.ts",
		"tail/one.go",
		"tail/one/two.py",
	} {
		writeFile(t, repository, relative, "fixture\n")
	}

	result := mustCollect(t, repository, ModeBuild)
	for _, relative := range []string{"foo/one/bar.go", "leading-one/deep.py", "special/drop.rs", "special/one/two/drop.rs", "nested/everywhere.ts", "tail/one.go", "tail/one"} {
		assertExclusion(t, result, relative, ExcludedIgnored)
	}
	for _, relative := range []string{"foo/one/two/bar.go", "leading-one/two/deep.py"} {
		found := false
		for _, candidate := range result.Paths {
			found = found || candidate.RelativePath == relative
		}
		if !found {
			t.Fatalf("ordinary consecutive stars crossed a slash for %q: %#v", relative, result)
		}
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

func TestParseGitIndexValidatesV2V3AndCompressedV4Entries(t *testing.T) {
	tests := []struct {
		name    string
		version uint32
		entries []gitIndexFixtureEntry
		want    map[string]uint32
	}{
		{name: "v2", version: 2, entries: []gitIndexFixtureEntry{{name: "a.go", mode: 0o100644}}, want: map[string]uint32{"a.go": 0o100644}},
		{name: "v3-extended", version: 3, entries: []gitIndexFixtureEntry{{name: "link.go", mode: 0o120000, extended: 0x2000}}, want: map[string]uint32{"link.go": 0o120000}},
		{name: "v4-compressed", version: 4, entries: []gitIndexFixtureEntry{{name: "directory/a.go", mode: 0o100755}, {name: "directory/b.go", mode: 0o100644}}, want: map[string]uint32{"directory/a.go": 0o100755, "directory/b.go": 0o100644}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			parsed, err := parseGitIndex(buildGitIndex(t, test.version, test.entries, nil))
			if err != nil {
				t.Fatal(err)
			}
			if len(parsed.paths) != len(test.want) {
				t.Fatalf("parsed paths = %#v, want modes %#v", parsed.paths, test.want)
			}
			for relative, wantMode := range test.want {
				if gotMode, ok := parsed.mode(relative); !ok || gotMode != wantMode {
					t.Fatalf("mode(%q) = %06o/%t, want %06o/true", relative, gotMode, ok, wantMode)
				}
			}
		})
	}
}

func TestParseGitIndexAcceptsRealGitV4WithMultiByteCompression(t *testing.T) {
	repository := t.TempDir()
	runGit := func(arguments ...string) {
		t.Helper()
		command := exec.Command("git", append([]string{"-C", repository}, arguments...)...)
		if output, err := command.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v: %s", arguments, err, output)
		}
	}
	runGit("init", "-q")
	longName := strings.Repeat("a", 200) + ".go"
	writeFile(t, repository, longName, "package long\n")
	writeFile(t, repository, "b.go", "package b\n")
	runGit("add", longName, "b.go")
	runGit("update-index", "--index-version=4")
	raw, err := os.ReadFile(filepath.Join(repository, ".git", "index"))
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := parseGitIndex(raw)
	if err != nil {
		t.Fatal(err)
	}
	longMode, longOK := parsed.mode(longName)
	shortMode, shortOK := parsed.mode("b.go")
	if !longOK || longMode != 0o100644 || !shortOK || shortMode != 0o100644 {
		t.Fatalf("real v4 paths/modes = %#v/%#v", parsed.paths, parsed.modes)
	}
}

func TestParseGitIndexRejectsInvalidFlagsModesNameLengthsPaddingAndOrder(t *testing.T) {
	valid := buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "alpha.go", mode: 0o100644}}, nil)
	tests := map[string][]byte{
		"invalid-mode":         buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "alpha.go", mode: 0o100664}}, nil),
		"v2-extended":          buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "alpha.go", mode: 0o100644, extended: 0x2000}}, nil),
		"v3-reserved-extended": buildGitIndex(t, 3, []gitIndexFixtureEntry{{name: "alpha.go", mode: 0o100644, extended: 0x0001}}, nil),
		"out-of-order":         buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "z.go", mode: 0o100644}, {name: "a.go", mode: 0o100644}}, nil),
	}
	badLength := append([]byte(nil), valid...)
	endian.BigEndian.PutUint16(badLength[12+60:12+62], 1)
	rewriteGitIndexChecksum(badLength)
	tests["name-length"] = badLength
	badPadding := append([]byte(nil), valid...)
	badPadding[12+62+len("alpha.go")+1] = 1
	rewriteGitIndexChecksum(badPadding)
	tests["padding"] = badPadding
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := parseGitIndex(raw); err == nil {
				t.Fatal("invalid index accepted")
			}
		})
	}
}

func TestParseGitIndexValidatesExtensionsChecksumAndEntryCount(t *testing.T) {
	entry := []gitIndexFixtureEntry{{name: "a.go", mode: 0o100644}}
	optional := buildGitIndex(t, 2, entry, []gitIndexFixtureExtension{{signature: "TREE", contents: []byte("optional")}})
	if _, err := parseGitIndex(optional); err != nil {
		t.Fatalf("optional uppercase extension rejected: %v", err)
	}
	mandatory := buildGitIndex(t, 2, entry, []gitIndexFixtureExtension{{signature: "abcd", contents: []byte("mandatory")}})
	if _, err := parseGitIndex(mandatory); err == nil {
		t.Fatal("unknown mandatory lowercase extension accepted")
	}
	split := buildGitIndex(t, 2, entry, []gitIndexFixtureExtension{{signature: "link", contents: make([]byte, 20)}})
	if _, err := parseGitIndex(split); !errors.Is(err, errSplitIndex) {
		t.Fatalf("split extension error = %v, want errSplitIndex", err)
	}
	badChecksum := append([]byte(nil), optional...)
	badChecksum[len(badChecksum)-1] ^= 0xff
	if _, err := parseGitIndex(badChecksum); err == nil {
		t.Fatal("invalid checksum accepted")
	}
	badCount := append([]byte(nil), optional...)
	endian.BigEndian.PutUint32(badCount[8:12], 2)
	rewriteGitIndexChecksum(badCount)
	if _, err := parseGitIndex(badCount); err == nil {
		t.Fatal("mismatched entry count accepted")
	}
}

func TestParseGitIndexRejectsBoundedDecodedPathsAndUnsafeComponents(t *testing.T) {
	tests := map[string]string{
		"individual-decoded-path": strings.Repeat("a", 4094) + ".go",
		"component-depth":         strings.Repeat("a/", 256) + "file.go",
		"root-git-component":      ".git/config",
		"nested-git-component":    "src/.GIT/config",
		"dot-component":           "src/./file.go",
		"parent-component":        "src/../file.go",
		"empty-component":         "src//file.go",
		"invalid-utf8":            "src/bad-\xff.go",
	}
	for name, relative := range tests {
		t.Run(name, func(t *testing.T) {
			raw := buildGitIndex(t, 4, []gitIndexFixtureEntry{{name: relative, mode: 0o100644}}, nil)
			if _, err := parseGitIndex(raw); err == nil {
				t.Fatalf("unsafe or unbounded decoded pathname accepted (bytes=%d)", len(relative))
			}
		})
	}
}

func TestParseGitIndexRejectsCompressedV4ExpansionBeforeConcatenation(t *testing.T) {
	first := strings.Repeat("a", 4088) + ".go"
	second := first + strings.Repeat("b", 128)
	raw := buildGitIndex(t, 4, []gitIndexFixtureEntry{
		{name: first, mode: 0o100644},
		{name: second, mode: 0o100644},
	}, nil)
	if _, err := parseGitIndex(raw); err == nil {
		t.Fatal("compressed v4 pathname expansion beyond the decoded-path bound was accepted")
	}
}

func TestParseGitIndexRejectsAggregateCompressedV4DecodedPathBytes(t *testing.T) {
	const decodedPathByteLimit = 16 << 20
	prefix := strings.Repeat("a", 3980) + "/"
	entryCount := decodedPathByteLimit/(len(prefix)+len("00000.go")) + 2
	entries := make([]gitIndexFixtureEntry, 0, entryCount)
	for index := 0; index < entryCount; index++ {
		entries = append(entries, gitIndexFixtureEntry{
			name: fmt.Sprintf("%s%05d.go", prefix, index),
			mode: 0o100644,
		})
	}
	raw := buildGitIndex(t, 4, entries, nil)
	if len(raw) >= decodedPathByteLimit/4 {
		t.Fatalf("hostile fixture is not meaningfully compressed: raw=%d decoded>%d", len(raw), decodedPathByteLimit)
	}
	if _, err := parseGitIndex(raw); err == nil {
		t.Fatal("aggregate compressed v4 decoded pathname expansion was accepted")
	}
}

func TestCollectPrunesGitlinkSubmoduleAndNeverIndexesItsFiles(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "module/source.go", "package module\n")
	writeFile(t, repository, "kept.go", "package kept\n")
	writeRawGitIndex(t, filepath.Join(repository, ".git"), buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "module", mode: 0o160000}}, nil))
	result := mustCollect(t, repository, ModeBuild)
	if got, want := result.Paths, []Path{{RelativePath: "kept.go", Language: "go", Size: 13}}; !samePathsIgnoringDigest(got, want) {
		t.Fatalf("gitlink inventory result = %#v", result)
	}
	assertExclusion(t, result, "module", ExcludedGit)
}

func TestCollectMarksSplitAndInvalidIndexesUnknownWithZeroCoverage(t *testing.T) {
	for _, test := range []struct {
		name    string
		index   []byte
		warning string
	}{
		{name: "invalid", index: []byte("DIRCbroken"), warning: "git-index-invalid"},
		{name: "split", index: buildGitIndex(t, 2, []gitIndexFixtureEntry{{name: "source.go", mode: 0o100644}}, []gitIndexFixtureExtension{{signature: "link", contents: make([]byte, 20)}}), warning: "git-index-split-unsupported"},
	} {
		t.Run(test.name, func(t *testing.T) {
			repository := newRepository(t)
			writeFile(t, repository, "source.go", "package source\n")
			writeFile(t, repository, "unknown.xyz", "unknown\n")
			writeRawGitIndex(t, filepath.Join(repository, ".git"), test.index)
			result := mustCollect(t, repository, ModeBuild)
			if !result.Partial || !result.UnknownRemainder || !contains(result.Warnings, test.warning) || result.Coverage.PathCoverage != 0 || result.Coverage.LanguageCoverage != 0 {
				t.Fatalf("unknown index coverage result = %#v", result)
			}
		})
	}
}

func TestCollectCoverageIsEquivalentForGitDirectoryAndPointerFile(t *testing.T) {
	directoryRepository := newRepository(t)
	writeFile(t, directoryRepository, "source.go", "package source\n")
	pointerRepository := newRepository(t)
	if err := os.Remove(filepath.Join(pointerRepository, ".git")); err != nil {
		t.Fatal(err)
	}
	gitDirectory := filepath.Join(t.TempDir(), "git-dir")
	if err := os.Mkdir(gitDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, pointerRepository, ".git", "gitdir: "+gitDirectory+"\n")
	writeFile(t, pointerRepository, "source.go", "package source\n")
	directoryResult := mustCollect(t, directoryRepository, ModeBuild)
	pointerResult := mustCollect(t, pointerRepository, ModeBuild)
	if !reflect.DeepEqual(directoryResult.Coverage, pointerResult.Coverage) {
		t.Fatalf("Git form coverage differs: directory=%#v pointer=%#v", directoryResult.Coverage, pointerResult.Coverage)
	}
}

func TestCollectMarksPostChildMutationPartial(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "a.go", "package a\n")
	writeFile(t, repository, "z.go", "package z\n")
	inventoryEntryHook = func(entry boundary.RepositoryEntry) {
		if entry.RelativePath == "z.go" {
			inventoryEntryHook = nil
			if err := os.WriteFile(filepath.Join(repository, "a.go"), []byte("package changed\n"), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	t.Cleanup(func() { inventoryEntryHook = nil })
	result := mustCollect(t, repository, ModeBuild)
	if !result.Partial || !result.UnknownRemainder || !contains(result.Warnings, "repository-changed-during-inventory") || result.Coverage.PathCoverage != 0 {
		t.Fatalf("post-child mutation result = %#v", result)
	}
}

func TestCollectBuildAndEstimateAreUnknownAfterGlobalSubtreeMutation(t *testing.T) {
	for _, mode := range []Mode{ModeBuild, ModeEstimate} {
		t.Run(string(mode), func(t *testing.T) {
			repository := newRepository(t)
			writeFile(t, repository, "a/early.go", "package early\n")
			writeFile(t, repository, "z.go", "package z\n")
			inventoryEntryHook = func(entry boundary.RepositoryEntry) {
				if entry.RelativePath == "z.go" {
					inventoryEntryHook = nil
					writeFile(t, repository, "a/early.go", "package changed\n")
				}
			}
			t.Cleanup(func() { inventoryEntryHook = nil })

			result := mustCollect(t, repository, mode)
			if !result.Partial || !result.UnknownRemainder || !contains(result.Warnings, "repository-changed-during-inventory") || result.Coverage.PathCoverage != 0 || result.Coverage.LanguageCoverage != 0 {
				t.Fatalf("%s global mutation result claimed exactness: %#v", mode, result)
			}
		})
	}
}

func TestCollectReportsActualBoundedRawIO(t *testing.T) {
	repository := newRepository(t)
	writeFile(t, repository, "source.go", strings.Repeat("x", 32*1024))
	writeFile(t, repository, "binary.go", "\x00"+strings.Repeat("x", 32*1024))
	roots := validatedRoots(t, repository)
	defer roots.Close()
	before := roots.IOObservation()
	result, err := Collect(roots, ModeEstimate)
	if err != nil {
		t.Fatal(err)
	}
	after := roots.IOObservation()
	if result.DirectoryEntries != after.ReadDirectoryEntries-before.ReadDirectoryEntries || result.PrefixBytes != after.ReadPrefixBytes-before.ReadPrefixBytes || result.FullBodyOpens != after.FullBodyOpens-before.FullBodyOpens || result.FullBodyBytes != after.FullBodyBytes-before.FullBodyBytes {
		t.Fatalf("inventory I/O counters are not boundary observations: result=%#v before=%#v after=%#v", result, before, after)
	}
	if result.DirectoryEntries != 6 || result.PrefixBytes != 2*uint64(binaryPrefixBytes) || result.FullBodyOpens != 0 || result.FullBodyBytes != 0 {
		t.Fatalf("estimate I/O was not bounded: %#v", result)
	}
}

func TestCollectBoundsIgnoreRulesAndPatternWork(t *testing.T) {
	repository := newRepository(t)
	var rules strings.Builder
	for index := 0; index <= maximumIgnoreRules; index++ {
		fmt.Fprintf(&rules, "ignored-%03d.go\n", index)
	}
	writeFile(t, repository, ".gitignore", rules.String())
	writeFile(t, repository, "source.go", "package source\n")
	result := mustCollect(t, repository, ModeBuild)
	if !result.Partial || !result.UnknownRemainder || !contains(result.Warnings, "gitignore-rule-limit") || result.Coverage.PathCoverage != 0 {
		t.Fatalf("bounded ignore policy result = %#v", result)
	}
}

func TestCollectStopsDeterministicallyAtAggregateIgnoreMatchBudget(t *testing.T) {
	repository := newRepository(t)
	var policyContents strings.Builder
	for index := 0; index < 64; index++ {
		fmt.Fprintf(&policyContents, "never-match-%03d.go\n", index)
	}
	writeFile(t, repository, ".gitignore", policyContents.String())
	for index := 0; index < 10; index++ {
		writeFile(t, repository, fmt.Sprintf("source-%02d.go", index), "package source\n")
	}
	collectOnce := func() Result {
		roots := validatedRoots(t, repository)
		defer roots.Close()
		result, err := collectWithIgnoreLimits(roots, ModeBuild, policy.ProductionLimits(), 100, 1<<20)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	first := collectOnce()
	second := collectOnce()
	if diff := cmpInventory(first, second); diff != "" {
		t.Fatalf("match-budget stop is nondeterministic: %s", diff)
	}
	if !first.Partial || !first.UnknownRemainder || !contains(first.Warnings, "gitignore-match-limit") || len(first.Paths) != 0 || first.FullBodyOpens != 0 {
		t.Fatalf("bounded ignore-match result = %#v", first)
	}
}

func TestIgnoreMatcherProductionBudgetsBoundActualRegexAttempts(t *testing.T) {
	makeRules := func() []ignoreRule {
		rules := make([]ignoreRule, maximumIgnoreRules)
		for index := range rules {
			pattern := fmt.Sprintf("never/%03d", index)
			matcher, ok := compileGitGlob(pattern)
			if !ok {
				t.Fatalf("compileGitGlob(%q) failed", pattern)
			}
			rules[index] = ignoreRule{pattern: pattern, directory: true, slash: true, matcher: matcher}
		}
		return rules
	}
	type observation struct {
		attempts int
		work     int
	}
	run := func(relative string, repeat bool) observation {
		budget := newIgnoreMatchBudget(maximumIgnoreRuleEvaluations, maximumIgnoreMatchWork)
		rules := makeRules()
		observed := observation{}
		budget.observe = func(pattern, candidate string) {
			observed.attempts++
			observed.work += (len(pattern) + 1) * (len(candidate) + 1)
		}
		for {
			_, limited := ignoredBy(rules, relative, true, budget)
			if limited {
				break
			}
			if !repeat {
				t.Fatal("depth-256 matcher did not exhaust its work budget")
			}
		}
		if observed.attempts > maximumIgnoreRuleEvaluations {
			t.Fatalf("actual regex attempts = %d, ceiling = %d", observed.attempts, maximumIgnoreRuleEvaluations)
		}
		if observed.work > maximumIgnoreMatchWork {
			t.Fatalf("actual regex work = %d, ceiling = %d", observed.work, maximumIgnoreMatchWork)
		}
		return observed
	}

	depth256 := strings.Repeat("d/", 255) + "d"
	first := run(depth256, false)
	second := run(depth256, false)
	if first != second || first.attempts == 0 || first.work == 0 {
		t.Fatalf("depth-256 slash-directory work is not deterministic: first=%#v second=%#v", first, second)
	}

	evaluationBound := run("a/b", true)
	if evaluationBound.attempts != maximumIgnoreRuleEvaluations {
		t.Fatalf("actual regex attempts = %d, want exact evaluation ceiling %d", evaluationBound.attempts, maximumIgnoreRuleEvaluations)
	}
}

func TestIgnoreMatcherChecksEachScopedNonSlashComponentOnce(t *testing.T) {
	rules := make([]ignoreRule, maximumIgnoreRules)
	for index := range rules {
		pattern := fmt.Sprintf("never-%03d", index)
		matcher, ok := compileGitGlob(pattern)
		if !ok {
			t.Fatalf("compileGitGlob(%q) failed", pattern)
		}
		rules[index] = ignoreRule{pattern: pattern, matcher: matcher}
	}
	relative := strings.Repeat("d/", 255) + "file.go"
	run := func() (attempts, work int, limited bool) {
		budget := newIgnoreMatchBudget(maximumIgnoreRuleEvaluations, maximumIgnoreMatchWork)
		budget.observe = func(pattern, candidate string) {
			attempts++
			work += (len(pattern) + 1) * (len(candidate) + 1)
		}
		_, limited = ignoredBy(rules, relative, false, budget)
		return attempts, work, limited
	}
	firstAttempts, firstWork, firstLimited := run()
	secondAttempts, secondWork, secondLimited := run()
	wantAttempts := maximumIgnoreRules * 256
	if firstLimited || secondLimited {
		t.Fatalf("depth-256 non-slash rules exhausted a production budget: first=%v second=%v", firstLimited, secondLimited)
	}
	if firstAttempts != wantAttempts || secondAttempts != wantAttempts || firstWork != secondWork {
		t.Fatalf("non-slash scoped work is not linear/deterministic: first=%d/%d second=%d/%d want attempts=%d", firstAttempts, firstWork, secondAttempts, secondWork, wantAttempts)
	}
}

func TestCollectDeepTraversalReturnsStablePartial(t *testing.T) {
	repository := newRepository(t)
	current := repository
	for depth := 0; depth < 257; depth++ {
		current = filepath.Join(current, "d")
		if err := os.Mkdir(current, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	writeFile(t, current, "too-deep.go", "package deep\n")
	result := mustCollect(t, repository, ModeBuild)
	if !result.Partial || !result.UnknownRemainder || !contains(result.Warnings, "inventory-traversal-limit") || result.Coverage.PathCoverage != 0 {
		t.Fatalf("deep traversal result = %#v", result)
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

func TestExtensionRegistryMatchesExtractorBackedFormats(t *testing.T) {
	registry := ExtensionRegistry()
	seen := map[string]bool{}
	for _, metadata := range registry {
		seen[metadata.Language] = true
	}
	for _, language := range []string{"go", "python", "javascript", "typescript", "rust", "markdown", "json", "toml"} {
		if !seen[language] {
			t.Fatalf("registry missing %s: %#v", language, registry)
		}
	}
	for _, relative := range []string{"config.json", "config.toml"} {
		if languageForPath(relative) == "" {
			t.Fatalf("registry/inventory mismatch for %s", relative)
		}
	}
	for _, relative := range []string{"config.yaml", "config.ini", "config.conf"} {
		if languageForPath(relative) != "" {
			t.Fatalf("registry unexpectedly accepts unbacked format %s", relative)
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
	if result.DirectoryEntries != 6 || result.PrefixBytes != uint64(2*len("package a\n")) || result.FullBodyOpens != 2 || result.FullBodyBytes != uint64(2*len("package a\n")) || len(result.Paths) > limits.MaximumEligiblePaths {
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
	entries := make([]gitIndexFixtureEntry, 0, len(names))
	for _, name := range names {
		entries = append(entries, gitIndexFixtureEntry{name: name, mode: 0o100644})
	}
	writeRawGitIndex(t, gitDirectory, buildGitIndex(t, 2, entries, nil))
}

type gitIndexFixtureEntry struct {
	name     string
	mode     uint32
	stage    uint16
	extended uint16
}

type gitIndexFixtureExtension struct {
	signature string
	contents  []byte
}

func buildGitIndex(t *testing.T, version uint32, entries []gitIndexFixtureEntry, extensions []gitIndexFixtureExtension) []byte {
	t.Helper()
	var raw bytes.Buffer
	raw.WriteString("DIRC")
	_ = endian.Write(&raw, endian.BigEndian, version)
	_ = endian.Write(&raw, endian.BigEndian, uint32(len(entries)))
	previous := ""
	for _, entry := range entries {
		start := raw.Len()
		header := make([]byte, 60)
		endian.BigEndian.PutUint32(header[24:28], entry.mode)
		raw.Write(header)
		nameLength := len(entry.name)
		if nameLength > 0x0fff {
			nameLength = 0x0fff
		}
		flags := uint16(nameLength) | entry.stage<<12
		if entry.extended != 0 {
			flags |= 0x4000
		}
		_ = endian.Write(&raw, endian.BigEndian, flags)
		if entry.extended != 0 {
			_ = endian.Write(&raw, endian.BigEndian, entry.extended)
		}
		if version == 4 {
			common := 0
			for common < len(previous) && common < len(entry.name) && previous[common] == entry.name[common] {
				common++
			}
			strip := len(previous) - common
			if strip >= 0x80 {
				t.Fatalf("fixture v4 strip is too large: %d", strip)
			}
			raw.WriteByte(byte(strip))
			raw.WriteString(entry.name[common:])
			raw.WriteByte(0)
		} else {
			raw.WriteString(entry.name)
			raw.WriteByte(0)
			for (raw.Len()-start)%8 != 0 {
				raw.WriteByte(0)
			}
		}
		previous = entry.name
	}
	for _, extension := range extensions {
		if len(extension.signature) != 4 {
			t.Fatalf("extension signature = %q, want four bytes", extension.signature)
		}
		raw.WriteString(extension.signature)
		_ = endian.Write(&raw, endian.BigEndian, uint32(len(extension.contents)))
		raw.Write(extension.contents)
	}
	digest := sha1.Sum(raw.Bytes())
	raw.Write(digest[:])
	return raw.Bytes()
}

func rewriteGitIndexChecksum(raw []byte) {
	digest := sha1.Sum(raw[:len(raw)-sha1.Size])
	copy(raw[len(raw)-sha1.Size:], digest[:])
}

func writeRawGitIndex(t *testing.T, gitDirectory string, raw []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(gitDirectory, "index"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
}
