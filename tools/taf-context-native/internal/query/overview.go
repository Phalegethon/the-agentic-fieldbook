package query

import (
	"cmp"
	"sort"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// The grouping and ranking constants live here alone. The wire only bounds how
// many rows a result may carry; how the table is shaped is this file's rule.
// No constant here counts rows: the table is as wide as the repository's own
// shape makes it, and a caller who cannot afford all of it says so with its
// output budget, which the consumer applies to the table this file returns.
const (
	// A group is replaced by its children while it holds more than 40 % of the
	// counted files, so one dominant directory cannot stand for a repository.
	overviewSplitNumerator   = 2
	overviewSplitDenominator = 5
	// A split never reaches deeper than four directory segments below the
	// overview root: below that a table stops describing and starts listing.
	maximumOverviewDepth = 4
	overviewRootPrefix   = "."
)

// File rank tiers, lower is better. Every counted file has one, so the file
// layer can order a repository completely rather than only its code.
const (
	overviewTierEntryPoint    = 0
	overviewTierCode          = 1
	overviewTierDocument      = 2
	overviewTierConfiguration = 3
	// overviewTierOther is the catch-all for a counted file whose admitted
	// records are none of a module, a definition, an entry point, a heading
	// or document chunk, or a configuration record — for example a file
	// whose only records are imports. Keeping it last keeps the file
	// ordering total: every counted file has a tier, ranked or not.
	overviewTierOther = 4
)

// wellKnownEntryNames are the base names a repository conventionally starts
// at. The list is a ranking hint and nothing more: a file it names keeps the
// record kinds it really has, and no new record kind is invented for it.
// main.go earns the hint only inside a cmd/ directory, where Go keeps its
// commands; anywhere else it is an ordinary file of its package.
var wellKnownEntryNames = [...]string{
	"__main__.py", "app.py", "cli.py", "index.js", "index.ts", "index.tsx",
	"layout.tsx", "lib.rs", "main.go", "main.py", "main.rs", "manage.py",
	"page.tsx", "server.ts",
}

// commandDirectory is the one directory segment that turns main.go into a
// well-known entry name.
const commandDirectory = "cmd"

// fileFacts is what one pass over a path's records establishes about the file
// itself: the language it is written in, what it defines, whether it is prose
// or configuration rather than code, and the record that stands for it.
type fileFacts struct {
	path            string
	relative        string
	language        string
	definitionCount int
	entryPointCount int
	document        bool
	configuration   bool
	wellKnown       bool
	tier            int
	// readmeRank keeps a README ahead of the other documents of its group and
	// is zero for every file outside the document tier.
	readmeRank     int
	representative rankedCandidate
}

// overviewDirectoryGroup is one row under construction: a prefix relative to
// the overview root, its depth in directory segments, the counted files it
// holds, and the counters those files add up to.
type overviewDirectoryGroup struct {
	prefix   string
	depth    int
	files    []int
	counters overviewCounters
}

type overviewCounters struct {
	files          int
	definitions    int
	entryPoints    int
	documents      int
	configurations int
}

// Overview answers how a repository is organized: the whole table of directory
// groups with counts, and a ranked file layer spread across those groups. It
// walks the path-sorted records once, charging one work-budget unit per record
// visited, and never resolves anything or reopens a file. The engine folds
// nothing away and reports no folded group, so a consumer that has to fit the
// answer into an output budget still has every row to choose from.
func Overview(snapshot store.Snapshot, request wire.Request, limits policy.Limits) Response {
	budget := newWorkBudget(limits, len(snapshot.Records))
	root, extraPrefixes := overviewRoot(request.Filters.PathPrefixes)
	rooted := false
	finish := func(records []model.Record, omitted int, partial bool, rows []wire.OverviewGroup, counted int) Response {
		response := budget.response(records, omitted, partial)
		response.Groups = rows
		// The engine never folds a row away, so the count of the groups a "*"
		// row stands for is zero here and stays the consumer's to raise.
		response.Overview = wire.OverviewSummary{Root: root, CountedFileCount: counted, OtherGroupCount: 0}
		response.ExtraPathPrefixes = extraPrefixes
		// A requested root the walk never reached a path under is named but
		// not a directory of this repository. A walk a budget cut short
		// reached only part of the paths, so it makes no such claim.
		response.RootUnmatched = root != "" && !rooted && !partial
		return response
	}
	// An empty snapshot examines nothing and is not exhausted.
	if budget.maximum < 1 {
		return finish([]model.Record{}, 0, false, []wire.OverviewGroup{}, 0)
	}
	files, partial, rooted := overviewFiles(snapshot, request, root, budget)
	groups := overviewDirectoryGroups(files)
	rows := make([]wire.OverviewGroup, 0, len(groups))
	for index := range groups {
		rankOverviewFiles(groups[index].files, files)
		rows = append(rows, overviewRow(root, groups[index], files, limits))
	}
	selected := selectOverviewFiles(groups, files, request.MaximumResults)
	return finish(selected, len(files)-len(selected), partial, rows, len(files))
}

// overviewRoot normalizes the requested subtree into a directory prefix a
// consumer can join a group prefix to. The wire caps path prefixes but does not
// forbid several, so the first in sorted order wins and the caller is told.
// The wire's own path grammar admits an interior "." segment, an empty
// segment from a doubled separator, and a trailing "." segment; trimming only
// the trailing separator would let one of those survive into the root and
// make the render step reject the very result this function built, so the
// root is rebuilt from the segments that name a real directory.
func overviewRoot(prefixes []string) (string, bool) {
	if len(prefixes) == 0 {
		return "", false
	}
	sorted := canonicalFilterValues(prefixes)
	segments := make([]string, 0, strings.Count(sorted[0], "/")+1)
	for _, segment := range strings.Split(sorted[0], "/") {
		if segment == "" || segment == overviewRootPrefix {
			continue
		}
		segments = append(segments, segment)
	}
	if len(segments) == 0 {
		return "", len(sorted) > 1
	}
	return strings.Join(segments, "/") + "/", len(sorted) > 1
}

// overviewFiles derives one fileFacts per counted path. Paths arrive from the
// map groups, so a path represented only by references contributes nothing,
// and a path whose records are all inadmissible is not counted at all. The
// last return value reports whether any indexed path lies under the root at
// all, which is a different question from how many of them were counted.
func overviewFiles(snapshot store.Snapshot, request wire.Request, root string, budget *workBudget) ([]fileFacts, bool, bool) {
	// The overview counts files, so the two symbol-shaped filters are dropped
	// before the shared record predicate is built: the wire already refuses
	// them for this operation, and honouring them here would drop a file whose
	// other records match. The path prefixes go with them because the root
	// check below is the stricter, segment-aware form of the same narrowing.
	admissible := request
	admissible.Filters.SymbolKinds, admissible.Filters.SourceTypes, admissible.Filters.PathPrefixes = nil, nil, nil
	predicate := newFilterPredicate(admissible)
	groups, partial := snapshot.Query.MapGroups()
	ordinals := snapshot.Query.PathOrdinals()
	files := make([]fileFacts, 0, len(groups))
	rooted := false
	for _, group := range groups {
		if !strings.HasPrefix(group.Path, root) {
			continue
		}
		rooted = true
		facts, counted, stopped := overviewFileFacts(snapshot, ordinals, group.Path, root, predicate, budget)
		// A walk the budget cut short proves nothing about the paths it never
		// reached, so the table it could build is reported as partial evidence.
		if stopped {
			return files, true, rooted
		}
		if counted {
			files = append(files, facts)
		}
	}
	return files, partial, rooted
}

// overviewFileFacts scans one path's slice of the canonical path index. Two raw
// paths can share one normalized path, so the range the binary search returns
// is still filtered by the exact path of every record.
func overviewFileFacts(snapshot store.Snapshot, ordinals []uint32, path, root string, predicate filterPredicate, budget *workBudget) (fileFacts, bool, bool) {
	facts := fileFacts{path: path, relative: strings.TrimPrefix(path, root)}
	start, end := relatedPathRange(snapshot.Records, ordinals, path)
	languages := make([]languageCount, 0, 2)
	admitted, structural, documents, configurations := 0, 0, 0, 0
	for index := start; index < end; index++ {
		if !budget.visitRecord() {
			return facts, false, true
		}
		ordinal := ordinals[index]
		if uint64(ordinal) >= uint64(len(snapshot.Records)) {
			budget.exhausted = true
			return facts, false, true
		}
		record := snapshot.Records[ordinal]
		if record.Path != path || !predicate.permits(record) {
			continue
		}
		candidate := newMapCandidate(record)
		if admitted == 0 || compareRepresentativeCandidate(candidate, facts.representative) < 0 {
			facts.representative = candidate
		}
		admitted++
		switch record.RecordKind {
		case model.Definition:
			facts.definitionCount++
		case model.EntryPoint:
			facts.entryPointCount++
		case model.Heading, model.DocumentChunk:
			documents++
		case model.Configuration:
			configurations++
		}
		if record.RecordKind == model.Module || definitionRecord(record.RecordKind) {
			structural++
		}
		languages = countLanguage(languages, record.Language)
	}
	if admitted == 0 {
		return facts, false, false
	}
	facts.language = dominantLanguage(languages)
	// A file is prose or configuration only when it carries nothing else; one
	// definition inside a document makes it code again.
	facts.document, facts.configuration = documents == admitted, configurations == admitted
	facts.wellKnown = wellKnownEntryPath(path)
	facts.tier, facts.readmeRank = overviewTier(facts, structural)
	return facts, true, false
}

type languageCount struct {
	language string
	count    int
}

// countLanguage tallies one record's language. A record without a language
// names none, and a language row must carry a name, so it is skipped.
func countLanguage(counts []languageCount, language string) []languageCount {
	if language == "" {
		return counts
	}
	for index := range counts {
		if counts[index].language == language {
			counts[index].count++
			return counts
		}
	}
	return append(counts, languageCount{language: language, count: 1})
}

// dominantLanguage is the language most of a file's records are written in,
// ties broken by name so the answer never depends on the record order.
func dominantLanguage(counts []languageCount) string {
	best := ""
	bestCount := 0
	for _, entry := range counts {
		if entry.count > bestCount || (entry.count == bestCount && (best == "" || entry.language < best)) {
			best, bestCount = entry.language, entry.count
		}
	}
	return best
}

// overviewTier places one file in the rank order of the file layer: entry
// points and the well-known names first, then the rest of the code, then prose
// with a README ahead of it, then configuration. A file that is none of these
// still has a tier, so no counted file is unrankable.
func overviewTier(facts fileFacts, structural int) (int, int) {
	switch {
	case facts.entryPointCount > 0 || facts.wellKnown:
		return overviewTierEntryPoint, 0
	case structural > 0:
		return overviewTierCode, 0
	case facts.document:
		if readmeFile(facts.path) {
			return overviewTierDocument, 0
		}
		return overviewTierDocument, 1
	case facts.configuration:
		return overviewTierConfiguration, 0
	default:
		return overviewTierOther, 0
	}
}

func readmeFile(path string) bool {
	return strings.HasPrefix(normalize(baseName(path)), "readme")
}

// wellKnownEntryPath reports whether a path's base name is one of the
// well-known entry names, with main.go admitted only below a cmd/ directory.
func wellKnownEntryPath(path string) bool {
	base := baseName(path)
	found := false
	for _, name := range wellKnownEntryNames {
		if name == base {
			found = true
			break
		}
	}
	if !found {
		return false
	}
	if base != "main.go" {
		return true
	}
	for _, segment := range strings.Split(directoryOf(path), "/") {
		if segment == commandDirectory {
			return true
		}
	}
	return false
}

func baseName(path string) string {
	if cut := strings.LastIndex(path, "/"); cut >= 0 {
		return path[cut+1:]
	}
	return path
}

func directoryOf(path string) string {
	if cut := strings.LastIndex(path, "/"); cut >= 0 {
		return path[:cut]
	}
	return ""
}

// overviewDirectoryGroups builds the adaptive-depth table: one group per
// top-level directory under the root, split while a group dominates, then
// ordered by what a reader is looking for — where the definitions are.
//
// The loop ends because every split replaces one group by children that are
// either a directory one segment deeper — which maximumOverviewDepth stops —
// or the "<dir>/." group of the files sitting directly inside it, which names
// no directory to descend into and is never splittable. Nothing else can end
// it: how many rows the table already holds is not part of the rule.
func overviewDirectoryGroups(files []fileFacts) []overviewDirectoryGroup {
	groups := childOverviewGroups("", 0, indexRange(len(files)), files)
	for {
		chosen := -1
		var children []overviewDirectoryGroup
		for index := range groups {
			if !splittableOverviewGroup(groups[index], len(files)) {
				continue
			}
			if chosen >= 0 && !largerOverviewGroup(groups[index], groups[chosen]) {
				continue
			}
			// A group whose only child is one directory describes exactly what
			// that directory describes, so it is replaced by it and the split
			// looks again from there; a lone "<dir>/." child names no
			// directory to descend into and only lengthens the prefix.
			candidates := childOverviewGroups(groups[index].prefix, groups[index].depth, groups[index].files, files)
			if len(candidates) < 2 && !descendableOverviewChild(candidates) {
				continue
			}
			chosen, children = index, candidates
		}
		if chosen < 0 {
			break
		}
		replaced := make([]overviewDirectoryGroup, 0, len(groups)+len(children)-1)
		replaced = append(replaced, groups[:chosen]...)
		replaced = append(replaced, children...)
		replaced = append(replaced, groups[chosen+1:]...)
		groups = replaced
	}
	for index := range groups {
		groups[index].counters = countOverviewGroup(groups[index], files)
	}
	sort.Slice(groups, func(left, right int) bool {
		return compareOverviewGroup(groups[left], groups[right]) < 0
	})
	return groups
}

func indexRange(count int) []int {
	indexes := make([]int, count)
	for index := range indexes {
		indexes[index] = index
	}
	return indexes
}

// childOverviewGroups partitions the files of one group by their next path
// segment. Files sitting directly inside the group form a "<dir>/." group of
// their own, and only when they exist.
func childOverviewGroups(parent string, depth int, indexes []int, files []fileFacts) []overviewDirectoryGroup {
	children := make([]overviewDirectoryGroup, 0, 8)
	byPrefix := make(map[string]int, 8)
	for _, index := range indexes {
		rest := files[index].relative[len(parent):]
		prefix, childDepth := parent+overviewRootPrefix, depth
		if cut := strings.Index(rest, "/"); cut >= 0 {
			prefix, childDepth = parent+rest[:cut+1], depth+1
		}
		position, exists := byPrefix[prefix]
		if !exists {
			position = len(children)
			byPrefix[prefix] = position
			children = append(children, overviewDirectoryGroup{prefix: prefix, depth: childDepth})
		}
		children[position].files = append(children[position].files, index)
	}
	return children
}

// splittableOverviewGroup applies the size and depth halves of the split rule.
// The half about the children themselves — at least two of them, or the one
// directory the split descends into — needs the children and is checked by
// the caller.
func splittableOverviewGroup(group overviewDirectoryGroup, counted int) bool {
	if !strings.HasSuffix(group.prefix, "/") || group.depth >= maximumOverviewDepth {
		return false
	}
	return len(group.files)*overviewSplitDenominator > counted*overviewSplitNumerator
}

// descendableOverviewChild reports whether a group's only child is a directory
// the split may descend into. Each descent adds a segment of depth, so
// splittableOverviewGroup ends the chain at maximumOverviewDepth.
func descendableOverviewChild(candidates []overviewDirectoryGroup) bool {
	return len(candidates) == 1 && strings.HasSuffix(candidates[0].prefix, "/")
}

// largerOverviewGroup breaks a tie on "largest" by path, so which group a split
// picks never depends on the order the groups happened to be built in. Since
// no ceiling can stop the loop early, every group above the threshold is split
// before it ends and this only decides the order the work is done in.
func largerOverviewGroup(candidate, current overviewDirectoryGroup) bool {
	if len(candidate.files) != len(current.files) {
		return len(candidate.files) > len(current.files)
	}
	return candidate.prefix < current.prefix
}

func countOverviewGroup(group overviewDirectoryGroup, files []fileFacts) overviewCounters {
	counters := overviewCounters{files: len(group.files)}
	for _, index := range group.files {
		counters.definitions += files[index].definitionCount
		counters.entryPoints += files[index].entryPointCount
		if files[index].document {
			counters.documents++
		}
		if files[index].configuration {
			counters.configurations++
		}
	}
	return counters
}

func compareOverviewGroup(left, right overviewDirectoryGroup) int {
	for _, comparison := range []int{
		cmp.Compare(right.counters.definitions, left.counters.definitions),
		cmp.Compare(right.counters.files, left.counters.files),
		cmp.Compare(left.prefix, right.prefix),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

// rankOverviewFiles orders one group's files the way the file layer reads them:
// by tier, a README ahead of the other documents, then by how much the file
// defines, then by path.
func rankOverviewFiles(indexes []int, files []fileFacts) {
	sort.Slice(indexes, func(left, right int) bool {
		return compareFileFacts(files[indexes[left]], files[indexes[right]]) < 0
	})
}

func compareFileFacts(left, right fileFacts) int {
	for _, comparison := range []int{
		cmp.Compare(left.tier, right.tier),
		cmp.Compare(left.readmeRank, right.readmeRank),
		cmp.Compare(right.definitionCount, left.definitionCount),
		cmp.Compare(left.path, right.path),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

// overviewRow renders one kept group. The prefix stays relative to the
// repository root, so a consumer never has to join it to the overview root.
func overviewRow(root string, group overviewDirectoryGroup, files []fileFacts, limits policy.Limits) wire.OverviewGroup {
	identity := files[group.files[0]].representative.record.Identity
	return wire.OverviewGroup{
		PathPrefix: root + group.prefix, Depth: group.depth,
		FileCount: group.counters.files, DefinitionCount: group.counters.definitions,
		EntryPointCount: group.counters.entryPoints, DocumentCount: group.counters.documents,
		ConfigurationCount:     group.counters.configurations,
		Languages:              overviewLanguages(group.files, files, limits),
		RepresentativeIdentity: &identity,
	}
}

// overviewLanguages counts the files of a group by language, most files first
// and ties by name. The list is bounded by the request's own injected limits,
// the same bound Overview already applies to its work budget, not by a fixed
// reference to the production limits — a caller enforcing a stricter cap must
// see it honoured here too.
func overviewLanguages(indexes []int, files []fileFacts, limits policy.Limits) []wire.OverviewLanguage {
	counts := make([]languageCount, 0, 4)
	for _, index := range indexes {
		counts = countLanguage(counts, files[index].language)
	}
	sort.Slice(counts, func(left, right int) bool {
		if counts[left].count != counts[right].count {
			return counts[left].count > counts[right].count
		}
		return counts[left].language < counts[right].language
	})
	if maximum := limits.MaximumCollectionItems; len(counts) > maximum {
		counts = counts[:maximum]
	}
	languages := make([]wire.OverviewLanguage, 0, len(counts))
	for _, entry := range counts {
		languages = append(languages, wire.OverviewLanguage{Language: entry.language, FileCount: entry.count})
	}
	return languages
}

// selectOverviewFiles spreads the file layer over the table: each round takes
// every group's next best file, so a dominant directory cannot fill the answer
// on its own and no group is left out of the rounds. maximum_results is what
// bounds the layer, however wide the table is.
func selectOverviewFiles(groups []overviewDirectoryGroup, files []fileFacts, maximum int) []model.Record {
	selected := make([]model.Record, 0, min(max(0, maximum), len(files)))
	for round := 0; len(selected) < maximum; round++ {
		offered := false
		for _, group := range groups {
			if round >= len(group.files) {
				continue
			}
			offered = true
			selected = append(selected, files[group.files[round]].representative.record)
			if len(selected) >= maximum {
				break
			}
		}
		if !offered {
			break
		}
	}
	return selected
}
