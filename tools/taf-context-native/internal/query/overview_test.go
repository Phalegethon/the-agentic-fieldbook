package query

import (
	"fmt"
	"math/rand"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// overviewSpec describes one indexed file for the fixture builder: its path,
// the language its records carry, and the record kinds the index holds for it.
// The overview counts files, so a fixture only has to say what each file's
// records are, never where they sit inside it.
type overviewSpec struct {
	path     string
	language string
	kinds    []model.RecordKind
}

func goFile(path string, kinds ...model.RecordKind) overviewSpec {
	return overviewSpec{path: path, language: "go", kinds: kinds}
}

func documentFile(path string) overviewSpec {
	return overviewSpec{path: path, language: "markdown", kinds: []model.RecordKind{model.Heading}}
}

func configurationFile(path string) overviewSpec {
	return overviewSpec{path: path, language: "json", kinds: []model.RecordKind{model.Configuration}}
}

// overviewFixture turns file specifications into the record set an index would
// carry for them. Identities are derived from the path so the fixture stays
// readable in a failure message.
func overviewFixture(specs ...overviewSpec) []model.Record {
	records := make([]model.Record, 0, len(specs)*2)
	for _, spec := range specs {
		for index, kind := range spec.kinds {
			records = append(records, model.Record{
				Identity: fmt.Sprintf("%s#%02d", spec.path, index), Path: spec.path,
				StartLine: index + 1, EndLine: index + 1, Language: spec.language,
				RecordKind: kind, SourceType: overviewSourceType(kind),
				QualifiedName:    strings.ReplaceAll(spec.path, "/", ".") + fmt.Sprintf(".%02d", index),
				ExtractionMethod: "test", EvidenceClass: model.Verified,
			})
		}
	}
	return records
}

func overviewSourceType(kind model.RecordKind) string {
	switch kind {
	case model.Heading, model.DocumentChunk:
		return "document"
	case model.Configuration:
		return "configuration"
	default:
		return "source"
	}
}

// overviewDirectory builds count Go files inside one directory, each carrying a
// module and a definition record, so a test can grow a directory to whatever
// share of the repository it needs.
func overviewDirectory(directory string, count int) []overviewSpec {
	specs := make([]overviewSpec, 0, count)
	for index := 0; index < count; index++ {
		specs = append(specs, goFile(fmt.Sprintf("%sfile%02d.go", directory, index), model.Module, model.Definition))
	}
	return specs
}

func overviewRequest(options ...func(*wire.Request)) wire.Request {
	request := wire.Request{
		SchemaVersion: "4", Operation: wire.RepositoryOverview,
		MaximumResults: 64, Filters: wire.Filters{},
	}
	for _, option := range options {
		option(&request)
	}
	return request
}

func withPathPrefixes(prefixes ...string) func(*wire.Request) {
	return func(request *wire.Request) { request.Filters.PathPrefixes = prefixes }
}

func withLanguages(languages ...string) func(*wire.Request) {
	return func(request *wire.Request) { request.Filters.Languages = languages }
}

func withMaximumResults(maximum int) func(*wire.Request) {
	return func(request *wire.Request) { request.MaximumResults = maximum }
}

func withAllowInferred(request *wire.Request) { request.AllowInferred = true }

func overviewOf(t *testing.T, specs []overviewSpec, options ...func(*wire.Request)) Response {
	t.Helper()
	return Overview(relatedSnapshot(overviewFixture(specs...)), overviewRequest(options...), policy.ProductionLimits())
}

func groupPrefixes(groups []wire.OverviewGroup) []string {
	prefixes := make([]string, len(groups))
	for index, group := range groups {
		prefixes[index] = group.PathPrefix
	}
	return prefixes
}

func groupNamed(t *testing.T, groups []wire.OverviewGroup, prefix string) wire.OverviewGroup {
	t.Helper()
	for _, group := range groups {
		if group.PathPrefix == prefix {
			return group
		}
	}
	t.Fatalf("group %q missing from %#v", prefix, groupPrefixes(groups))
	return wire.OverviewGroup{}
}

func recordPaths(records []model.Record) []string {
	paths := make([]string, len(records))
	for index, record := range records {
		paths[index] = record.Path
	}
	return paths
}

// The files at the repository root are a group of their own, and a group's
// counters describe the files it holds rather than the records behind them.
func TestOverviewGroupsRootFilesUnderTheDotPrefix(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		documentFile("readme.md"),
		goFile("main.go", model.Module, model.EntryPoint),
		goFile("pkg/a.go", model.Module, model.Definition),
	})
	if response.Partial {
		t.Fatalf("response = %#v", response)
	}
	if got, want := groupPrefixes(response.Groups), []string{"pkg/", "."}; !reflect.DeepEqual(got, want) {
		t.Fatalf("group prefixes = %#v, want %#v", got, want)
	}
	root := groupNamed(t, response.Groups, ".")
	if root.Depth != 0 || root.FileCount != 2 || root.DefinitionCount != 0 || root.EntryPointCount != 1 || root.DocumentCount != 1 || root.ConfigurationCount != 0 {
		t.Fatalf("root group = %#v", root)
	}
	if got, want := root.Languages, []wire.OverviewLanguage{{Language: "go", FileCount: 1}, {Language: "markdown", FileCount: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("root languages = %#v, want %#v", got, want)
	}
	if root.RepresentativeIdentity == nil || *root.RepresentativeIdentity != "main.go#00" {
		t.Fatalf("root representative = %#v", root.RepresentativeIdentity)
	}
	code := groupNamed(t, response.Groups, "pkg/")
	if code.Depth != 1 || code.FileCount != 1 || code.DefinitionCount != 1 || code.EntryPointCount != 0 {
		t.Fatalf("pkg group = %#v", code)
	}
	if want := (wire.OverviewSummary{Root: "", CountedFileCount: 3, OtherGroupCount: 0}); response.Overview != want {
		t.Fatalf("summary = %#v, want %#v", response.Overview, want)
	}
	if got, want := recordPaths(response.Records), []string{"pkg/a.go", "main.go", "readme.md"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("file layer = %#v, want %#v", got, want)
	}
}

// A directory holding more than 40 % of the counted files is replaced by its
// children, and the files sitting directly inside it become their own group.
func TestOverviewSplitsADirectoryHoldingMoreThanFortyPercent(t *testing.T) {
	specs := append(overviewDirectory("big/x/", 3), overviewDirectory("big/y/", 2)...)
	specs = append(specs, goFile("big/z.go", model.Module))
	for _, directory := range []string{"d1/", "d2/", "d3/", "d4/"} {
		specs = append(specs, overviewDirectory(directory, 1)...)
	}
	response := overviewOf(t, specs)
	prefixes := groupPrefixes(response.Groups)
	sorted := append([]string(nil), prefixes...)
	sort.Strings(sorted)
	if got, want := sorted, []string{"big/.", "big/x/", "big/y/", "d1/", "d2/", "d3/", "d4/"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("group prefixes = %#v, want %#v", got, want)
	}
	direct := groupNamed(t, response.Groups, "big/.")
	if direct.Depth != 1 || direct.FileCount != 1 {
		t.Fatalf("big/. group = %#v", direct)
	}
	if child := groupNamed(t, response.Groups, "big/x/"); child.Depth != 2 || child.FileCount != 3 {
		t.Fatalf("big/x/ group = %#v", child)
	}
	if response.Overview.CountedFileCount != 10 {
		t.Fatalf("counted = %d, want 10", response.Overview.CountedFileCount)
	}
}

// The split rule is "more than 40 %", not "at least 40 %": a group holding
// exactly two fifths of the counted files stays whole, and one file above
// that share is replaced by its children.
func TestOverviewSplitsOnlyStrictlyAboveFortyPercent(t *testing.T) {
	for _, testCase := range []struct {
		name        string
		bigChildren [2]int
		singletons  int
		split       bool
	}{
		{name: "exactly forty percent stays whole", bigChildren: [2]int{2, 2}, singletons: 6, split: false},
		{name: "one file above forty percent splits", bigChildren: [2]int{3, 2}, singletons: 5, split: true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			specs := append(overviewDirectory("big/p/", testCase.bigChildren[0]), overviewDirectory("big/q/", testCase.bigChildren[1])...)
			for index := 0; index < testCase.singletons; index++ {
				specs = append(specs, overviewDirectory(fmt.Sprintf("d%02d/", index), 1)...)
			}
			response := overviewOf(t, specs)
			prefixes := groupPrefixes(response.Groups)
			whole := slices.Contains(prefixes, "big/")
			if whole == testCase.split {
				t.Fatalf("groups = %#v, want big/ split = %v", prefixes, testCase.split)
			}
			if testCase.split {
				if !slices.Contains(prefixes, "big/p/") || !slices.Contains(prefixes, "big/q/") {
					t.Fatalf("groups = %#v, want big/p/ and big/q/", prefixes)
				}
			}
		})
	}
}

// Ties on the largest splittable group break by path ascending, so the
// smaller-prefix group always splits first, however many groups tie.
func TestOverviewBreaksLargestGroupTiesByPath(t *testing.T) {
	specs := append(overviewDirectory("aa/p/", 10), overviewDirectory("aa/q/", 9)...)
	specs = append(specs, overviewDirectory("bb/p/", 10)...)
	specs = append(specs, overviewDirectory("bb/q/", 9)...)
	for index := 0; index < 9; index++ {
		specs = append(specs, overviewDirectory(fmt.Sprintf("d%02d/", index), 1)...)
	}
	response := overviewOf(t, specs)
	prefixes := groupPrefixes(response.Groups)
	if !slices.Contains(prefixes, "aa/p/") || !slices.Contains(prefixes, "aa/q/") {
		t.Fatalf("aa/ must split first on the path tie-break: %#v", prefixes)
	}
	if !slices.Contains(prefixes, "bb/") {
		t.Fatalf("bb/ must stay whole once the ceiling is reached: %#v", prefixes)
	}
	if slices.Contains(prefixes, "aa/") || slices.Contains(prefixes, "bb/p/") || slices.Contains(prefixes, "bb/q/") {
		t.Fatalf("exactly one group splits before the ceiling: %#v", prefixes)
	}
}

// A directory with a single child is never replaced by it: the split rule asks
// for at least two children, so an overview cannot walk down a chain of
// single-child directories and still describe one group.
func TestOverviewKeepsADirectoryWithASingleChild(t *testing.T) {
	response := overviewOf(t, overviewDirectory("a/b/", 4))
	if got, want := groupPrefixes(response.Groups), []string{"a/"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("group prefixes = %#v, want %#v", got, want)
	}
}

// Splitting stops four directory segments below the overview root, however
// dominant the deepest group still is.
func TestOverviewStopsSplittingAtDepthFour(t *testing.T) {
	specs := []overviewSpec{
		goFile("a/x.go", model.Module),
		goFile("a/b/x.go", model.Module),
		goFile("a/b/c/x.go", model.Module),
		goFile("a/b/c/d/x.go", model.Module),
	}
	specs = append(specs, overviewDirectory("a/b/c/d/e/", 10)...)
	response := overviewOf(t, specs)
	prefixes := append([]string(nil), groupPrefixes(response.Groups)...)
	sort.Strings(prefixes)
	if got, want := prefixes, []string{"a/.", "a/b/.", "a/b/c/.", "a/b/c/d/"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("group prefixes = %#v, want %#v", got, want)
	}
	deepest := groupNamed(t, response.Groups, "a/b/c/d/")
	if deepest.Depth != 4 || deepest.FileCount != 11 {
		t.Fatalf("deepest group = %#v", deepest)
	}
}

// Splitting stops once the table reaches twelve groups, so a dominant
// directory below a wide root stays whole and is still reported as one row.
func TestOverviewStopsSplittingAtTwelveGroups(t *testing.T) {
	for _, testCase := range []struct {
		name       string
		singletons int
		split      bool
	}{
		{name: "eleven singletons reach the ceiling", singletons: 11, split: false},
		{name: "ten singletons leave room for one split", singletons: 10, split: true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			specs := append(overviewDirectory("big/x/", 15), overviewDirectory("big/y/", 15)...)
			for index := 0; index < testCase.singletons; index++ {
				specs = append(specs, overviewDirectory(fmt.Sprintf("d%02d/", index), 1)...)
			}
			response := overviewOf(t, specs)
			prefixes := groupPrefixes(response.Groups)
			whole := false
			for _, prefix := range prefixes {
				if prefix == "big/" {
					whole = true
				}
			}
			if whole == testCase.split {
				t.Fatalf("groups = %#v, want big/ split = %v", prefixes, testCase.split)
			}
			if testCase.split {
				if child := groupNamed(t, response.Groups, "big/x/"); child.FileCount != 15 {
					t.Fatalf("big/x/ = %#v", child)
				}
			}
		})
	}
}

// Beyond sixteen groups the surplus is folded into one "*" row whose counters
// are sums, whose languages are merged and re-sorted, and which represents no
// single file.
func TestOverviewFoldsTheSurplusIntoTheOtherRow(t *testing.T) {
	specs := make([]overviewSpec, 0, 210)
	for directory := 1; directory <= 20; directory++ {
		language := "go"
		if directory == 1 {
			language = "python"
		}
		for index := 0; index < directory; index++ {
			specs = append(specs, overviewSpec{
				path:     fmt.Sprintf("d%02d/file%02d.src", directory, index),
				language: language,
				kinds:    []model.RecordKind{model.Module, model.Definition},
			})
		}
	}
	response := overviewOf(t, specs)
	if len(response.Groups) != 17 {
		t.Fatalf("groups = %#v", groupPrefixes(response.Groups))
	}
	if got, want := groupPrefixes(response.Groups)[0], "d20/"; got != want {
		t.Fatalf("first group = %q, want %q", got, want)
	}
	if got, want := response.Groups[16].PathPrefix, "*"; got != want {
		t.Fatalf("last group = %q, want %q", got, want)
	}
	other := response.Groups[16]
	if other.Depth != 0 || other.FileCount != 10 || other.DefinitionCount != 10 || other.RepresentativeIdentity != nil {
		t.Fatalf("other row = %#v", other)
	}
	if got, want := other.Languages, []wire.OverviewLanguage{{Language: "go", FileCount: 9}, {Language: "python", FileCount: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("other languages = %#v, want %#v", got, want)
	}
	if want := (wire.OverviewSummary{Root: "", CountedFileCount: 210, OtherGroupCount: 4}); response.Overview != want {
		t.Fatalf("summary = %#v, want %#v", response.Overview, want)
	}
	// The folded directories keep their files out of the file layer.
	for _, path := range recordPaths(response.Records) {
		if strings.HasPrefix(path, "d01/") || strings.HasPrefix(path, "d04/") {
			t.Fatalf("folded directory reached the file layer: %q", path)
		}
	}
}

// A path prefix narrows the overview to a subtree: the groups are that
// subtree's children, the summary names the normalized root, and the group
// prefixes stay relative to the repository root.
func TestOverviewNarrowsToARequestedPathPrefix(t *testing.T) {
	specs := []overviewSpec{
		goFile("tools/top.go", model.Module),
		goFile("tools/a/x.go", model.Module, model.Definition),
		goFile("tools/b/y.go", model.Module),
		goFile("other/z.go", model.Module, model.Definition),
	}
	for _, prefix := range []string{"tools", "tools/"} {
		t.Run(prefix, func(t *testing.T) {
			response := overviewOf(t, specs, withPathPrefixes(prefix))
			if got, want := groupPrefixes(response.Groups), []string{"tools/a/", "tools/.", "tools/b/"}; !reflect.DeepEqual(got, want) {
				t.Fatalf("group prefixes = %#v, want %#v", got, want)
			}
			if got := groupNamed(t, response.Groups, "tools/a/"); got.Depth != 1 {
				t.Fatalf("depth is counted below the overview root: %#v", got)
			}
			if want := (wire.OverviewSummary{Root: "tools/", CountedFileCount: 3, OtherGroupCount: 0}); response.Overview != want {
				t.Fatalf("summary = %#v, want %#v", response.Overview, want)
			}
		})
	}
}

// The repository root can be named explicitly; it narrows nothing.
func TestOverviewTreatsADotPrefixAsTheRepositoryRoot(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		goFile("pkg/a.go", model.Module),
		goFile("top.go", model.Module),
	}, withPathPrefixes("."))
	if response.Overview.Root != "" || response.Overview.CountedFileCount != 2 {
		t.Fatalf("summary = %#v", response.Overview)
	}
}

// A prefix that is a directory name only by accident narrows nothing it does
// not name: "tools" is not the parent of "toolsmith/".
func TestOverviewPrefixMatchesWholeDirectorySegmentsOnly(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		goFile("tools/a.go", model.Module),
		goFile("toolsmith/b.go", model.Module),
	}, withPathPrefixes("tools"))
	if response.Overview.CountedFileCount != 1 {
		t.Fatalf("counted = %d, want 1", response.Overview.CountedFileCount)
	}
}

// The wire caps path prefixes but does not forbid several. The first in sorted
// order wins and the caller is told, so the answer stays one honest subtree.
func TestOverviewRootsAtTheFirstOfSeveralPrefixes(t *testing.T) {
	specs := []overviewSpec{goFile("alpha/a.go", model.Module), goFile("beta/b.go", model.Module)}
	response := overviewOf(t, specs, withPathPrefixes("beta/", "alpha/"))
	if response.Overview.Root != "alpha/" || response.Overview.CountedFileCount != 1 {
		t.Fatalf("summary = %#v", response.Overview)
	}
	if !response.ExtraPathPrefixes {
		t.Fatal("several prefixes must be reported to the caller")
	}
	single := overviewOf(t, specs, withPathPrefixes("alpha/"))
	if single.ExtraPathPrefixes {
		t.Fatal("a single prefix is not an extra prefix")
	}
}

// The language filter decides which files are counted at all, so a file whose
// records are all in another language leaves no trace in the table.
func TestOverviewCountsOnlyTheRequestedLanguages(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		goFile("pkg/a.go", model.Module, model.Definition),
		{path: "pkg/b.py", language: "python", kinds: []model.RecordKind{model.Module, model.Definition}},
		{path: "scripts/c.py", language: "python", kinds: []model.RecordKind{model.Module}},
	}, withLanguages("go"))
	if got, want := groupPrefixes(response.Groups), []string{"pkg/"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("group prefixes = %#v, want %#v", got, want)
	}
	if group := groupNamed(t, response.Groups, "pkg/"); group.FileCount != 1 {
		t.Fatalf("pkg group = %#v", group)
	}
	if response.Overview.CountedFileCount != 1 {
		t.Fatalf("counted = %d, want 1", response.Overview.CountedFileCount)
	}
}

// A file is a document or a configuration file only when it carries nothing
// else; one definition makes it code again.
func TestOverviewClassifiesFilesByTheirRecords(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		documentFile("group/readme.md"),
		configurationFile("group/settings.json"),
		{path: "group/mixed.md", language: "markdown", kinds: []model.RecordKind{model.Heading, model.Definition}},
		goFile("group/service.go", model.Module, model.Definition, model.Definition, model.EntryPoint),
	})
	group := groupNamed(t, response.Groups, "group/")
	if group.FileCount != 4 || group.DocumentCount != 1 || group.ConfigurationCount != 1 || group.DefinitionCount != 3 || group.EntryPointCount != 1 {
		t.Fatalf("group = %#v", group)
	}
}

// The representative of a file is the record the repository map would show for
// it, and only admissible records count: a file whose records are all inferred
// is not counted at all unless the request allows inferred evidence.
func TestOverviewPicksTheRepresentativeOfEachCountedFile(t *testing.T) {
	records := overviewFixture(
		goFile("pkg/a.go", model.Definition, model.Module),
		goFile("pkg/b.go", model.Module),
	)
	for index := range records {
		if records[index].Path == "pkg/b.go" {
			records[index].EvidenceClass = model.Inferred
		}
	}
	snapshot := relatedSnapshot(records)
	verified := Overview(snapshot, overviewRequest(), policy.ProductionLimits())
	if got := groupNamed(t, verified.Groups, "pkg/"); got.FileCount != 1 {
		t.Fatalf("inferred file was counted: %#v", got)
	}
	// The module record represents the file even though the definition sits
	// first in the fixture: the map's kind tier, not the record order, decides.
	if got := *groupNamed(t, verified.Groups, "pkg/").RepresentativeIdentity; got != "pkg/a.go#01" {
		t.Fatalf("representative = %q, want pkg/a.go#01", got)
	}
	inferred := Overview(snapshot, overviewRequest(withAllowInferred), policy.ProductionLimits())
	if got := groupNamed(t, inferred.Groups, "pkg/"); got.FileCount != 2 {
		t.Fatalf("allow_inferred group = %#v", got)
	}
	if got, want := recordPaths(inferred.Records), []string{"pkg/a.go", "pkg/b.go"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("allow_inferred file layer = %#v, want %#v", got, want)
	}
}

// Tier 0 is entry points and the well-known entry base names, tier 1 the rest
// of the code, tier 2 documents with README first, tier 3 configuration.
// "0-guide.md" sorts before "README.md" by path alone, so README leading it
// proves the readmeRank tie-break is doing the work, not the path order.
func TestOverviewRanksFilesByTier(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		configurationFile("group/a-settings.json"),
		documentFile("group/0-guide.md"),
		documentFile("group/README.md"),
		goFile("group/c-service.go", model.Module, model.Definition),
		goFile("group/d-boot.go", model.Module, model.EntryPoint),
		goFile("group/index.ts", model.Module),
	})
	want := []string{
		"group/d-boot.go", "group/index.ts", "group/c-service.go",
		"group/README.md", "group/0-guide.md", "group/a-settings.json",
	}
	if got := recordPaths(response.Records); !reflect.DeepEqual(got, want) {
		t.Fatalf("file layer = %#v, want %#v", got, want)
	}
}

// Every well-known base name is a ranking hint on its own, and main.go is one
// only inside a command directory.
func TestOverviewRanksEveryWellKnownEntryName(t *testing.T) {
	names := []string{"main.py", "__main__.py", "app.py", "manage.py", "cli.py", "index.js", "index.ts", "index.tsx", "page.tsx", "layout.tsx", "server.ts", "main.rs", "lib.rs"}
	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			response := overviewOf(t, []overviewSpec{
				goFile("group/a-plain.go", model.Module, model.Definition),
				goFile("group/"+name, model.Module),
			})
			if got, want := recordPaths(response.Records)[0], "group/"+name; got != want {
				t.Fatalf("first file = %q, want %q", got, want)
			}
		})
	}
	t.Run("main.go under a command directory", func(t *testing.T) {
		response := overviewOf(t, []overviewSpec{
			goFile("cmd/tool/a-plain.go", model.Module, model.Definition),
			goFile("cmd/tool/main.go", model.Module),
		})
		if got, want := recordPaths(response.Records)[0], "cmd/tool/main.go"; got != want {
			t.Fatalf("first file = %q, want %q", got, want)
		}
	})
	t.Run("main.go elsewhere", func(t *testing.T) {
		response := overviewOf(t, []overviewSpec{
			goFile("pkg/a-plain.go", model.Module, model.Definition),
			goFile("pkg/main.go", model.Module),
		})
		if got, want := recordPaths(response.Records)[0], "pkg/a-plain.go"; got != want {
			t.Fatalf("first file = %q, want %q", got, want)
		}
	})
}

// A counted file whose admitted records are none of module, definition,
// entry-point, heading/document-chunk or configuration still needs a place in
// the ranking, so it falls to the fifth "other" tier, last of every tier.
func TestOverviewRanksAFileWithNoStructuralRecordsLast(t *testing.T) {
	response := overviewOf(t, []overviewSpec{
		goFile("pkg/a.go", model.Module, model.Definition),
		documentFile("pkg/r.md"),
		configurationFile("pkg/c.json"),
		{path: "pkg/imports.go", language: "go", kinds: []model.RecordKind{model.Import, model.Import}},
	})
	want := []string{"pkg/a.go", "pkg/r.md", "pkg/c.json", "pkg/imports.go"}
	if got := recordPaths(response.Records); !reflect.DeepEqual(got, want) {
		t.Fatalf("file layer = %#v, want %#v", got, want)
	}
	if group := groupNamed(t, response.Groups, "pkg/"); group.FileCount != 4 {
		t.Fatalf("pkg group = %#v, want 4 counted files including the imports-only one", group)
	}
}

// The well-known list is a constant of the engine, so it is pinned here rather
// than only exercised through the ranking.
func TestOverviewWellKnownEntryNamesAreExact(t *testing.T) {
	want := []string{"__main__.py", "app.py", "cli.py", "index.js", "index.ts", "index.tsx", "layout.tsx", "lib.rs", "main.go", "main.py", "main.rs", "manage.py", "page.tsx", "server.ts"}
	if got := append([]string(nil), wellKnownEntryNames[:]...); !reflect.DeepEqual(got, want) {
		t.Fatalf("well-known names = %#v, want %#v", got, want)
	}
}

// The file layer spreads over the groups: each round takes one file from every
// group in table order, so a dominant directory cannot fill the answer.
func TestOverviewSelectsFilesRoundRobinAcrossGroups(t *testing.T) {
	specs := make([]overviewSpec, 0, 9)
	for _, directory := range []string{"a/", "b/", "c/"} {
		specs = append(specs, overviewDirectory(directory, 3)...)
	}
	response := overviewOf(t, specs)
	want := []string{
		"a/file00.go", "b/file00.go", "c/file00.go",
		"a/file01.go", "b/file01.go", "c/file01.go",
		"a/file02.go", "b/file02.go", "c/file02.go",
	}
	if got := recordPaths(response.Records); !reflect.DeepEqual(got, want) {
		t.Fatalf("file layer = %#v, want %#v", got, want)
	}
	if response.Omitted != 0 {
		t.Fatalf("omitted = %d, want 0", response.Omitted)
	}
}

// maximum_results bounds the file layer alone; every counted file the layer
// left out is an omission, including the files of the folded "*" row.
func TestOverviewBoundsTheFileLayerByMaximumResults(t *testing.T) {
	specs := make([]overviewSpec, 0, 9)
	for _, directory := range []string{"a/", "b/", "c/"} {
		specs = append(specs, overviewDirectory(directory, 3)...)
	}
	response := overviewOf(t, specs, withMaximumResults(4))
	want := []string{"a/file00.go", "b/file00.go", "c/file00.go", "a/file01.go"}
	if got := recordPaths(response.Records); !reflect.DeepEqual(got, want) {
		t.Fatalf("file layer = %#v, want %#v", got, want)
	}
	if response.Omitted != 5 || response.Partial {
		t.Fatalf("omitted = %d partial = %v, want 5 and false", response.Omitted, response.Partial)
	}
	if len(response.Groups) != 3 {
		t.Fatalf("the group table is never trimmed: %#v", groupPrefixes(response.Groups))
	}
}

// The answer depends on the index alone, never on the order the records
// reached it or on a map iteration.
func TestOverviewIsDeterministic(t *testing.T) {
	specs := []overviewSpec{
		documentFile("readme.md"), configurationFile("settings.json"),
		goFile("cmd/tool/main.go", model.Module, model.EntryPoint),
	}
	specs = append(specs, overviewDirectory("internal/a/", 3)...)
	specs = append(specs, overviewDirectory("internal/b/", 2)...)
	records := overviewFixture(specs...)
	first := Overview(relatedSnapshot(records), overviewRequest(), policy.ProductionLimits())
	for attempt := 0; attempt < 4; attempt++ {
		shuffled := append([]model.Record(nil), records...)
		rand.New(rand.NewSource(int64(attempt))).Shuffle(len(shuffled), func(i, j int) {
			shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
		})
		other := Overview(relatedSnapshot(shuffled), overviewRequest(), policy.ProductionLimits())
		if !reflect.DeepEqual(first.Groups, other.Groups) || first.Overview != other.Overview {
			t.Fatalf("attempt %d table = %#v / %#v", attempt, other.Groups, other.Overview)
		}
		if !reflect.DeepEqual(identities(first.Records), identities(other.Records)) {
			t.Fatalf("attempt %d file layer = %#v", attempt, recordPaths(other.Records))
		}
	}
}

// A path index that runs off the end of the record slice stops the walk: the
// overview reports partial evidence rather than a table built from a snapshot
// it could not read to the end.
func TestOverviewMarksPartialWhenThePathIndexRunsOffTheEnd(t *testing.T) {
	records := []model.Record{
		goRecord("m-module", "cmd/main.go", "main", model.Module, 1, 5),
		goRecord("m-run", "cmd/main.go", "main.Run", model.EntryPoint, 2, 4),
		goRecord("a-module", "pkg/a.go", "a", model.Module, 1, 5),
		goRecord("a-first", "pkg/a.go", "a.First", model.Definition, 2, 4),
	}
	if response := Overview(changedTruncatedSnapshot(records, 2), overviewRequest(), policy.ProductionLimits()); !response.Partial {
		t.Fatalf("truncated snapshot = %#v, want a partial response", response)
	}
	// The same snapshot answers without the flag once the walk can finish, so
	// the report above is the guard rather than a blind spot.
	if response := Overview(relatedSnapshot(records), overviewRequest(), policy.ProductionLimits()); response.Partial {
		t.Fatalf("complete snapshot = %#v, want a complete response", response)
	}
}

// A repository with more paths than the map frontier carries is described from
// the paths the frontier holds, and the answer says so.
func TestOverviewReportsPartialBeyondTheMapFrontier(t *testing.T) {
	paths := policy.ProductionLimits().MaximumLexicalCandidates + 1
	specs := make([]overviewSpec, 0, paths)
	for index := 0; index < paths; index++ {
		specs = append(specs, goFile(fmt.Sprintf("pkg/file%05d.go", index), model.Module))
	}
	response := overviewOf(t, specs)
	if !response.Partial {
		t.Fatalf("frontier = %#v", response.Overview)
	}
	if response.Overview.CountedFileCount != paths-1 {
		t.Fatalf("counted = %d, want %d", response.Overview.CountedFileCount, paths-1)
	}
}

// The overview visits every record of the paths it counts exactly once, and
// the shared budget floor is four times the record count, so a walk over a
// consistent snapshot is never cut short by the budget.
func TestOverviewWalksEveryRecordWithinTheWorkBudget(t *testing.T) {
	specs := append(overviewDirectory("pkg/", 6), documentFile("readme.md"), configurationFile("settings.json"))
	records := overviewFixture(specs...)
	response := Overview(relatedSnapshot(records), overviewRequest(), policy.ProductionLimits())
	if response.Partial || response.Counters.ConsideredRecords != len(records) {
		t.Fatalf("considered %d of %d records, partial = %v", response.Counters.ConsideredRecords, len(records), response.Partial)
	}
}

// An empty snapshot, and a root no indexed file sits under, both answer with an
// empty table rather than with nothing at all.
func TestOverviewAnswersAnEmptyRootWithAnEmptyTable(t *testing.T) {
	for _, testCase := range []struct {
		name    string
		specs   []overviewSpec
		options []func(*wire.Request)
		root    string
	}{
		{name: "empty snapshot", root: ""},
		{name: "root without files", specs: []overviewSpec{goFile("pkg/a.go", model.Module)}, options: []func(*wire.Request){withPathPrefixes("docs/")}, root: "docs/"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			response := overviewOf(t, testCase.specs, testCase.options...)
			if response.Groups == nil || len(response.Groups) != 0 || response.Records == nil || len(response.Records) != 0 {
				t.Fatalf("response = %#v", response)
			}
			if want := (wire.OverviewSummary{Root: testCase.root}); response.Overview != want {
				t.Fatalf("summary = %#v, want %#v", response.Overview, want)
			}
		})
	}
}

// The wire refuses the two symbol-shaped filters for this operation, so the
// overview must not silently honour them either: it counts files, and a file
// is not a symbol kind.
func TestOverviewIgnoresSymbolShapedFilters(t *testing.T) {
	specs := []overviewSpec{
		goFile("pkg/a.go", model.Module, model.Definition),
		documentFile("docs/guide.md"),
		configurationFile("settings.json"),
	}
	plain := overviewOf(t, specs)
	filtered := overviewOf(t, specs, func(request *wire.Request) {
		request.Filters.SymbolKinds = []string{"definition"}
		request.Filters.SourceTypes = []string{"source"}
	})
	if !reflect.DeepEqual(plain.Groups, filtered.Groups) || plain.Overview != filtered.Overview {
		t.Fatalf("filtered table = %#v / %#v", filtered.Groups, filtered.Overview)
	}
	if !reflect.DeepEqual(identities(plain.Records), identities(filtered.Records)) {
		t.Fatalf("filtered file layer = %#v", recordPaths(filtered.Records))
	}
}

// A group's language list is bounded by the limits the caller injects, not by
// a hard-coded reference to the production limits, so a caller enforcing a
// stricter cap sees it honoured rather than silently capped at 64.
func TestOverviewLanguagesHonourTheInjectedLimit(t *testing.T) {
	specs := []overviewSpec{
		{path: "pkg/a.go", language: "go", kinds: []model.RecordKind{model.Module}},
		{path: "pkg/b.py", language: "python", kinds: []model.RecordKind{model.Module}},
		{path: "pkg/c.rs", language: "rust", kinds: []model.RecordKind{model.Module}},
	}
	limits := policy.ProductionLimits()
	limits.MaximumCollectionItems = 2
	response := Overview(relatedSnapshot(overviewFixture(specs...)), overviewRequest(), limits)
	group := groupNamed(t, response.Groups, "pkg/")
	if got, want := group.Languages, []wire.OverviewLanguage{{Language: "go", FileCount: 1}, {Language: "python", FileCount: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("languages = %#v, want the two kept under the injected limit of 2: %#v", got, want)
	}
}

// A reference record is a use of a name, never a structure, so a path carrying
// nothing else is not a file the overview counts.
func TestOverviewSkipsPathsRepresentedOnlyByReferences(t *testing.T) {
	records := append(overviewFixture(goFile("pkg/a.go", model.Module)),
		goReference("pkg/b.go#00", "pkg/b.go", "b", 1, 2, []model.ReferenceEntry{{Name: "load", Line: 1, Count: 1}}))
	response := Overview(relatedSnapshot(records), overviewRequest(), policy.ProductionLimits())
	if response.Overview.CountedFileCount != 1 {
		t.Fatalf("counted = %d, want 1", response.Overview.CountedFileCount)
	}
}
