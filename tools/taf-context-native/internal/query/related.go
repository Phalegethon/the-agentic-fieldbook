package query

import (
	"cmp"
	"slices"
	"sort"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// RelatedFinding is one resolved edge: the definition, module, or import
// record at the far end, how the two are related, how well the resolution is
// evidenced, and where the use that produced the edge sits.
type RelatedFinding struct {
	Record         model.Record
	Relation       string
	EdgeEvidence   model.EvidenceClass
	ReferenceLine  int
	ReferenceCount int
}

// RelatedResponse carries the ranked edges. Unknown reports an anchor that is
// not a record a relationship may start from, which the caller answers with a
// refusal rather than with an empty finding list.
type RelatedResponse struct {
	Findings []RelatedFinding
	Omitted  int
	Partial  bool
	Unknown  bool
}

const (
	relationCall   = "call"
	relationImport = "import"
	// maximumInferredCandidates bounds how far a name-only resolution fans
	// out, so a common name cannot flood a result with guesses.
	maximumInferredCandidates = 8
	// wildcardBinding is the local name an import that binds every exported
	// name of a module carries.
	wildcardBinding = "*"
)

// Related answers one relationship question about up to a handful of anchor
// records. Every edge is resolved at query time from the file the use was
// written in, so a change in one file never invalidates another file's
// records.
func Related(snapshot store.Snapshot, request wire.Request, limits policy.Limits) RelatedResponse {
	budget := newWorkBudget(limits, len(snapshot.Records))
	empty := RelatedResponse{Findings: []RelatedFinding{}}
	if budget.maximum < 1 {
		return empty
	}
	resolver := newRelatedResolver(snapshot, budget)
	collector := &relatedCollector{
		request: request,
		edges:   make(map[string]relatedEdge, 16),
		ranking: newBoundedRanking(request.MaximumResults, budget),
	}
	for _, identity := range request.ResultIdentities {
		ordinal, found := anchorOrdinal(snapshot.Records, identity)
		if !found {
			return RelatedResponse{Findings: []RelatedFinding{}, Unknown: true}
		}
		anchor := snapshot.Records[ordinal]
		if !relationshipAnchor(anchor.RecordKind) {
			return RelatedResponse{Findings: []RelatedFinding{}, Unknown: true}
		}
		switch deref(request.Direction) {
		case "callers":
			resolver.callers(collector, ordinal, anchor)
		case "callees":
			resolver.callees(collector, anchor)
		case "importers":
			resolver.importers(collector, ordinal, anchor)
		case "imports":
			resolver.imports(collector, anchor)
		default:
			return empty
		}
	}
	selected, omitted := collector.ranking.records()
	findings := make([]RelatedFinding, 0, len(selected))
	for _, record := range selected {
		edge := collector.edges[record.Identity]
		findings = append(findings, RelatedFinding{
			Record: record, Relation: edge.relation, EdgeEvidence: edge.evidence,
			ReferenceLine: edge.line, ReferenceCount: edge.count,
		})
	}
	sortRelatedFindings(findings)
	partial := resolver.partial || collector.partial || budget.exhausted
	return RelatedResponse{Findings: findings, Omitted: omitted, Partial: partial}
}

// sortRelatedFindings restores the ranking order over the final edge evidence.
// An edge is offered to the ranking as soon as it is resolved, so a later,
// better-evidenced edge to the same record can upgrade it after it was placed;
// re-sorting the selected findings costs nothing and keeps the promise that
// verified edges come before inferred ones.
func sortRelatedFindings(findings []RelatedFinding) {
	sort.SliceStable(findings, func(left, right int) bool {
		return compareRelatedFinding(findings[left], findings[right]) < 0
	})
}

func compareRelatedFinding(left, right RelatedFinding) int {
	return compareRankedCandidate(
		newRankedCandidate(left.Record, relatedEdgeTier(left.EdgeEvidence)),
		newRankedCandidate(right.Record, relatedEdgeTier(right.EdgeEvidence)),
	)
}

// relationshipAnchor names the record kinds a relationship question may start
// from. A reference or an import describes a use, not a symbol, so it is
// refused like an identity that is not in the index at all.
func relationshipAnchor(kind model.RecordKind) bool {
	return kind == model.Definition || kind == model.EntryPoint || kind == model.Module
}

// definitionRecord names the kinds a target name may resolve to. An
// entry-point is a definition that also starts a program, so it resolves like
// one.
func definitionRecord(kind model.RecordKind) bool {
	return kind == model.Definition || kind == model.EntryPoint
}

func relatedEdgeTier(evidence model.EvidenceClass) int {
	if evidence == model.Verified {
		return 0
	}
	return 1
}

// anchorOrdinal locates one anchor in the identity-ordered record slice. An
// identity that is absent or carried by more than one record is not a usable
// anchor, exactly as it is not a usable snippet.
func anchorOrdinal(records []model.Record, identity string) (uint32, bool) {
	index := sort.Search(len(records), func(index int) bool { return records[index].Identity >= identity })
	if index == len(records) || records[index].Identity != identity {
		return 0, false
	}
	if (index > 0 && records[index-1].Identity == identity) || (index+1 < len(records) && records[index+1].Identity == identity) {
		return 0, false
	}
	return uint32(index), true
}

type relatedEdge struct {
	record   model.Record
	relation string
	evidence model.EvidenceClass
	line     int
	count    int
}

// relatedCollector keeps one edge per related record and offers each new
// record to the ranking as soon as it is resolved, the way Search admits a
// posting entry. Interleaving is what keeps an exhausted budget from throwing
// away everything that was already resolved; the map only deduplicates.
type relatedCollector struct {
	request wire.Request
	edges   map[string]relatedEdge
	ranking boundedRanking
	partial bool
}

func (collector *relatedCollector) add(edge relatedEdge) {
	if !collector.permits(edge) {
		return
	}
	current, exists := collector.edges[edge.record.Identity]
	if exists {
		if betterRelatedEdge(edge, current) {
			collector.edges[edge.record.Identity] = edge
		}
		return
	}
	collector.edges[edge.record.Identity] = edge
	if !collector.ranking.offer(edge.record, relatedEdgeTier(edge.evidence)) {
		collector.partial = true
	}
}

// permits hides an edge the request did not ask to see. Both halves of the
// claim must be verified by default: the record the engine found and the
// resolution that connected it to the anchor.
func (collector *relatedCollector) permits(edge relatedEdge) bool {
	if !collector.request.AllowInferred && (edge.evidence != model.Verified || edge.record.EvidenceClass != model.Verified) {
		return false
	}
	return matchesFilters(edge.record, collector.request.Filters)
}

// betterRelatedEdge prefers the better-evidenced edge and, at equal evidence,
// the earlier use, so two anchors or two table entries that reach the same
// record produce one stable answer.
func betterRelatedEdge(candidate, current relatedEdge) bool {
	if candidate.evidence != current.evidence {
		return candidate.evidence == model.Verified
	}
	return candidate.line < current.line
}

// relatedFile groups one file's records. language is the language every record
// of the file is written in, and is the only language a name written in this
// file may resolve to. complete is false when the work budget cut the grouping
// short, which stops any resolution reading it from claiming to be verified.
type relatedFile struct {
	definitions []uint32
	imports     []uint32
	references  []uint32
	module      int
	language    string
	complete    bool
}

type relatedResolution struct {
	candidates []uint32
	evidence   model.EvidenceClass
	resolved   bool
}

type relatedResolver struct {
	snapshot    store.Snapshot
	budget      *workBudget
	files       map[string]*relatedFile
	targets     map[string]relatedResolution
	candidates  map[string]relatedCandidates
	named       map[string]namedDefinitionScan
	modules     map[string]int
	fileModules map[string]string
	partial     bool
}

func newRelatedResolver(snapshot store.Snapshot, budget *workBudget) *relatedResolver {
	return &relatedResolver{
		snapshot: snapshot, budget: budget,
		files:       make(map[string]*relatedFile, 8),
		targets:     make(map[string]relatedResolution, 32),
		candidates:  make(map[string]relatedCandidates, 32),
		named:       make(map[string]namedDefinitionScan, 32),
		modules:     make(map[string]int, 8),
		fileModules: make(map[string]string, 8),
	}
}

func (resolver *relatedResolver) records() []model.Record { return resolver.snapshot.Records }

func (resolver *relatedResolver) visit(ordinal uint32) (model.Record, bool) {
	if !resolver.budget.visitRecord() {
		resolver.partial = true
		return model.Record{}, false
	}
	record, ok := resolver.peek(ordinal)
	if !ok {
		resolver.budget.exhausted = true
		resolver.partial = true
	}
	return record, ok
}

// peek reads a record the caller has already paid for. Re-reading an ordinal a
// scan has just charged is free; only the scan itself is bounded.
func (resolver *relatedResolver) peek(ordinal uint32) (model.Record, bool) {
	if uint64(ordinal) >= uint64(len(resolver.snapshot.Records)) {
		return model.Record{}, false
	}
	return resolver.snapshot.Records[ordinal], true
}

// file groups one file's records by kind. The canonical path index already
// keeps a file's records together and in a stable order, so the grouping is a
// bounded scan of that range and is charged to the work budget once per file.
func (resolver *relatedResolver) file(path string) *relatedFile {
	if cached, exists := resolver.files[path]; exists {
		return cached
	}
	view := &relatedFile{module: -1, complete: true}
	ordinals := resolver.snapshot.Query.PathOrdinals()
	start, end := relatedPathRange(resolver.snapshot.Records, ordinals, path)
	for index := start; index < end; index++ {
		ordinal := ordinals[index]
		record, ok := resolver.visit(ordinal)
		if !ok {
			view.complete = false
			break
		}
		if record.Path != path {
			continue
		}
		if view.language == "" {
			view.language = record.Language
		}
		switch {
		case record.RecordKind == model.Reference:
			view.references = append(view.references, ordinal)
		case record.RecordKind == model.Import:
			view.imports = append(view.imports, ordinal)
		case record.RecordKind == model.Module:
			if view.module < 0 {
				view.module = int(ordinal)
			}
		case definitionRecord(record.RecordKind):
			view.definitions = append(view.definitions, ordinal)
		}
	}
	resolver.files[path] = view
	return view
}

// relatedPathRange is the half-open range of the canonical path index that
// holds one file. Two raw paths can share a normalized path, so the caller
// still compares the exact path of every record in the range.
func relatedPathRange(records []model.Record, ordinals []uint32, path string) (int, int) {
	normalized := normalize(path)
	at := func(index int) string {
		ordinal := ordinals[index]
		if uint64(ordinal) >= uint64(len(records)) {
			return "\U0010ffff"
		}
		return normalize(records[ordinal].Path)
	}
	start := sort.Search(len(ordinals), func(index int) bool { return at(index) >= normalized })
	end := sort.Search(len(ordinals), func(index int) bool { return at(index) > normalized })
	return start, end
}

// resolveTarget maps a name as it is written inside one file to the
// definitions it can mean, following the three rules of the design: the names
// visible at the use, then the file's imports, then the name alone. Results are
// memoized per file, enclosing scope, and name; the scans behind them are
// memoized per file and name alone, so widening a call site's scope never costs
// another pass over a posting.
func (resolver *relatedResolver) resolveTarget(path, enclosing, target string) relatedResolution {
	key := path + "\x00" + enclosing + "\x00" + target
	if cached, exists := resolver.targets[key]; exists {
		return cached
	}
	resolution := resolver.resolve(path, enclosing, target)
	resolver.targets[key] = resolution
	return resolution
}

// resolve applies the three resolution rules. Rule 1 reads "the same file" as
// the same module scope - the definitions the file itself carries and the
// definitions of the same module in the same directory, which is one file in
// the languages where a file is a module and a package in Go - narrowed to the
// names actually visible where the use is written. Rule 2 follows the file's
// imports. A name both rules answer is ambiguous, so its edge is inferred; a
// name neither answers falls to rule 3, the name alone.
func (resolver *relatedResolver) resolve(path, enclosing, target string) relatedResolution {
	short := lastNameSegment(normalize(target))
	if short == "" {
		return relatedResolution{}
	}
	view := resolver.file(path)
	candidates := resolver.candidatesFor(view, path, target, short)
	scope := mergeOrdinals(candidates.local, candidates.scoped)
	// Visibility narrows a bare name only. A dotted target names its own scope,
	// and matchesTargetName has already required the definition's qualified
	// name to end with it, which is the stronger claim of the two.
	if !strings.Contains(normalize(target), ".") {
		scope = resolver.visibleOnly(scope, candidates.module, enclosing)
	}
	complete := view.complete && candidates.complete
	switch {
	case len(scope) == 1 && len(candidates.imported) == 0:
		return resolvedTo(scope, complete)
	case len(candidates.imported) == 1 && len(scope) == 0:
		return resolvedTo(append([]uint32(nil), candidates.imported...), complete)
	case len(scope) != 0 || len(candidates.imported) != 0:
		return resolver.inferredTo(mergeOrdinals(scope, candidates.imported))
	}
	named, _ := resolver.definitionsNamed(short, target, view.language)
	if len(named) == 0 {
		return relatedResolution{}
	}
	return resolver.inferredTo(append([]uint32(nil), named...))
}

// resolvedTo answers a single candidate. A scan the work budget cut short has
// proved nothing about the candidates it never reached, so it may name the one
// it found but never claim the resolution was unambiguous.
func resolvedTo(candidates []uint32, complete bool) relatedResolution {
	evidence := model.Verified
	if !complete {
		evidence = model.Inferred
	}
	return relatedResolution{candidates: candidates, evidence: evidence, resolved: true}
}

func (resolver *relatedResolver) inferredTo(candidates []uint32) relatedResolution {
	resolver.sortByPath(candidates)
	if len(candidates) > maximumInferredCandidates {
		candidates = candidates[:maximumInferredCandidates]
	}
	return relatedResolution{candidates: candidates, evidence: model.Inferred, resolved: true}
}

func mergeOrdinals(left, right []uint32) []uint32 {
	output := make([]uint32, 0, len(left)+len(right))
	output = append(output, left...)
	for _, ordinal := range right {
		if !slices.Contains(output, ordinal) {
			output = append(output, ordinal)
		}
	}
	return output
}

// relatedCandidates is what one name written in one file can mean before the
// call site is taken into account: the definitions the file carries, those of
// its module scope, and those its imports bind. Only the visibility filter
// depends on where the name is written, and that filter reads records the scans
// already paid for, so the whole set is memoized per file and name.
type relatedCandidates struct {
	module   string
	local    []uint32
	scoped   []uint32
	imported []uint32
	complete bool
}

func (resolver *relatedResolver) candidatesFor(view *relatedFile, path, target, short string) relatedCandidates {
	key := path + "\x00" + target
	if cached, exists := resolver.candidates[key]; exists {
		return cached
	}
	built := relatedCandidates{module: resolver.fileModuleName(path), complete: true}
	for _, ordinal := range view.definitions {
		record, ok := resolver.visit(ordinal)
		if !ok {
			built.complete = false
			break
		}
		if record.Language == view.language && matchesTargetName(record, target, short) {
			built.local = append(built.local, ordinal)
		}
	}
	if built.complete && built.module != "" {
		named, scanned := resolver.definitionsNamed(short, target, view.language)
		built.scoped = resolver.restrictDefinitions(named, built.module, pathDirectory(path))
		built.complete = scanned
	}
	if built.complete {
		module, directory, found, scanned := resolver.importedModuleOf(view, target)
		built.complete = scanned
		if found {
			named, ok := resolver.definitionsNamed(short, target, view.language)
			built.imported = resolver.restrictDefinitions(named, module, directory)
			if len(built.imported) == 0 && directory != "" {
				built.imported = resolver.restrictDefinitions(named, module, "")
			}
			built.complete = built.complete && ok
		}
	}
	resolver.candidates[key] = built
	return built
}

// visibleOnly keeps the candidates a bare name written at one call site could
// actually reach. The records were charged to the budget by the scans that
// produced the candidates, so narrowing them is free.
func (resolver *relatedResolver) visibleOnly(ordinals []uint32, module, enclosing string) []uint32 {
	output := make([]uint32, 0, len(ordinals))
	for _, ordinal := range ordinals {
		record, ok := resolver.peek(ordinal)
		if !ok {
			continue
		}
		if visibleAtScope(record.QualifiedName, module, enclosing) {
			output = append(output, ordinal)
		}
	}
	return output
}

// restrictDefinitions narrows an already-scanned candidate set to one module
// and, for a specifier naming a neighbour, one directory.
func (resolver *relatedResolver) restrictDefinitions(ordinals []uint32, module, directory string) []uint32 {
	output := make([]uint32, 0, len(ordinals))
	for _, ordinal := range ordinals {
		record, ok := resolver.peek(ordinal)
		if !ok {
			continue
		}
		if module != "" && recordModuleName(record) != module {
			continue
		}
		if directory != "" && pathDirectory(record.Path) != directory {
			continue
		}
		output = append(output, ordinal)
	}
	return output
}

// visibleAtScope reports whether a definition can be reached by a bare name
// written at one place in its file: a definition of the file's own module
// scope, or one nested in the chain of scopes enclosing the use. A class method
// is neither, so a method never answers a bare call written outside its class -
// a call that does name the class, or a receiver, writes a dotted target and is
// not asked this question at all.
func visibleAtScope(qualified, module, enclosing string) bool {
	parent := parentScopeName(normalize(qualified))
	if parent == "" {
		// A definition whose qualified name carries no scope sits at the top
		// level of its file.
		return true
	}
	if parent == module {
		return true
	}
	scope := normalize(enclosing)
	if scope == "" {
		return false
	}
	return scope == parent || strings.HasPrefix(scope, parent+".")
}

// parentScopeName is the qualified name of the scope one definition sits in,
// and is empty for a name that carries no scope at all.
func parentScopeName(name string) string {
	index := strings.LastIndexByte(name, '.')
	if index < 0 {
		return ""
	}
	return name[:index]
}

// importedModuleOf names the module a dotted target's first segment, or a
// plain target under a single wildcard import, was imported from. The second
// value is the directory the import resolves in: a module named relative to
// the importing file can only be that file's neighbour, which is what tells
// two same-named modules of one repository apart. It is empty when the
// specifier is absolute, and the caller then searches the whole index. The
// fourth value reports whether the file's imports were all examined.
func (resolver *relatedResolver) importedModuleOf(view *relatedFile, target string) (string, string, bool, bool) {
	first := firstNameSegment(normalize(target))
	wildcards := 0
	wildcardModule, wildcardDirectory := "", ""
	for _, ordinal := range view.imports {
		record, ok := resolver.visit(ordinal)
		if !ok {
			return "", "", false, false
		}
		module := importedModuleName(record)
		if module == "" {
			continue
		}
		directory := importDirectory(record)
		bound := boundImportName(record)
		if bound == wildcardBinding {
			wildcards++
			wildcardModule, wildcardDirectory = module, directory
			continue
		}
		if bound == first {
			return module, directory, true, true
		}
	}
	if wildcards == 1 && !strings.Contains(normalize(target), ".") {
		return wildcardModule, wildcardDirectory, true, true
	}
	return "", "", false, true
}

// importDirectory is the directory an import specifier resolves in: the
// importing file's own for a neighbouring specifier, and none otherwise, which
// leaves the whole index to search.
func importDirectory(record model.Record) string {
	if neighbouringImport(record) {
		return pathDirectory(record.Path)
	}
	return ""
}

// neighbouringImport reports whether a specifier names a module in the
// importing file's own directory: a single leading dot in Python, "./" in the
// JavaScript module systems. A parent-relative or absolute specifier names
// somewhere else and carries no directory hint.
func neighbouringImport(record model.Record) bool {
	specifier := record.TargetName
	switch record.Language {
	case "python":
		return strings.HasPrefix(specifier, ".") && !strings.HasPrefix(specifier, "..")
	case "javascript", "typescript":
		return strings.HasPrefix(specifier, "./")
	default:
		return false
	}
}

// definitionsNamed collects every definition carrying one short name in one
// language. The scan is memoized and the resolution rules narrow its result by
// module and directory in memory, so one posting is walked once per name and
// language however many files ask about it. A Rust macro definition keeps the
// "!" its call sites do not write, so its own short key is looked up as well.
// The second value reports whether the scan finished: a truncated scan has not
// seen the candidates that would make the name ambiguous, so its caller may not
// call the resolution verified.
func (resolver *relatedResolver) definitionsNamed(short, target, language string) ([]uint32, bool) {
	key := language + "\x00" + short + "\x00" + target
	if cached, exists := resolver.named[key]; exists {
		return cached.ordinals, cached.complete
	}
	output := make([]uint32, 0, 4)
	complete := true
	postings := [][]uint32{resolver.snapshot.Query.ShortOrdinals(short)}
	if macro := short + "!"; macro != short {
		postings = append(postings, resolver.snapshot.Query.ShortOrdinals(macro))
	}
scan:
	for _, posting := range postings {
		for _, ordinal := range posting {
			record, ok := resolver.visit(ordinal)
			if !ok {
				complete = false
				break scan
			}
			if !definitionRecord(record.RecordKind) || !matchesTargetName(record, target, short) {
				continue
			}
			// A name written in one language never means a definition written
			// in another; the two files share nothing but the spelling.
			if language != "" && record.Language != language {
				continue
			}
			if !slices.Contains(output, ordinal) {
				output = append(output, ordinal)
			}
		}
	}
	resolver.named[key] = namedDefinitionScan{ordinals: output, complete: complete}
	return output, complete
}

type namedDefinitionScan struct {
	ordinals []uint32
	complete bool
}

func (resolver *relatedResolver) sortByPath(ordinals []uint32) {
	records := resolver.snapshot.Records
	sort.SliceStable(ordinals, func(left, right int) bool {
		return compareRelatedRecord(records[ordinals[left]], records[ordinals[right]]) < 0
	})
}

func compareRelatedRecord(left, right model.Record) int {
	for _, comparison := range []int{
		cmp.Compare(normalize(left.Path), normalize(right.Path)),
		cmp.Compare(left.Path, right.Path),
		cmp.Compare(left.StartLine, right.StartLine),
		cmp.Compare(normalize(left.QualifiedName), normalize(right.QualifiedName)),
		cmp.Compare(left.Identity, right.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

// fileModuleName is the module one file belongs to: its module record where
// the language emits one, and otherwise the module part the extractors put in
// front of every qualified name of the file.
func (resolver *relatedResolver) fileModuleName(path string) string {
	if cached, exists := resolver.fileModules[path]; exists {
		return cached
	}
	name := resolver.readFileModuleName(path)
	resolver.fileModules[path] = name
	return name
}

func (resolver *relatedResolver) readFileModuleName(path string) string {
	view := resolver.file(path)
	if view.module >= 0 {
		if record, ok := resolver.visit(uint32(view.module)); ok {
			return normalize(lastNameSegment(record.QualifiedName))
		}
		return ""
	}
	for _, ordinals := range [][]uint32{view.definitions, view.references} {
		for _, ordinal := range ordinals {
			record, ok := resolver.visit(ordinal)
			if !ok {
				return ""
			}
			name := normalize(record.QualifiedName)
			if module := qualifyingModuleName(name); module != "" {
				return module
			}
			// A module-level reference is hosted by the module itself, so its
			// bare qualified name is that module's name.
			if record.RecordKind == model.Reference && name != "" {
				return name
			}
		}
	}
	return ""
}

// pathDirectory is the directory part of a repository-relative path, kept with
// its separator so one directory is never a prefix of another.
func pathDirectory(path string) string {
	if index := strings.LastIndexByte(path, '/'); index >= 0 {
		return normalize(path[:index+1])
	}
	return ""
}

// moduleNamed resolves a module name to the one module the index has under
// it. A Go package carries one module record per file, so the answer is a
// whole directory: the name is unambiguous when every module record carrying
// it lives in one directory, and the record reported for it is the first of
// that directory in path order. A name spread over two directories names two
// modules and can carry no verified claim.
func (resolver *relatedResolver) moduleNamed(name, language string) (int, bool) {
	key := language + "\x00" + name
	if cached, exists := resolver.modules[key]; exists {
		return cached, cached >= 0
	}
	candidates := make([]uint32, 0, 4)
	directory := ""
	ambiguous := false
	for _, ordinal := range resolver.snapshot.Query.ShortOrdinals(name) {
		record, ok := resolver.visit(ordinal)
		if !ok {
			ambiguous = true
			break
		}
		if record.RecordKind != model.Module || normalize(lastNameSegment(record.QualifiedName)) != name {
			continue
		}
		if language != "" && record.Language != language {
			continue
		}
		if len(candidates) != 0 && pathDirectory(record.Path) != directory {
			ambiguous = true
			break
		}
		directory = pathDirectory(record.Path)
		candidates = append(candidates, ordinal)
	}
	found := -1
	if !ambiguous && len(candidates) != 0 {
		resolver.sortByPath(candidates)
		found = int(candidates[0])
	}
	resolver.modules[key] = found
	return found, found >= 0
}

// callers walks the reference records keyed by the anchor's own short name and
// keeps those whose use of that name resolves, from their own file, to the
// anchor.
func (resolver *relatedResolver) callers(collector *relatedCollector, anchor uint32, record model.Record) {
	short := lastNameSegment(normalize(record.QualifiedName))
	if short == "" {
		return
	}
	// A Rust macro is defined as "name!" and called as "name", so the call
	// sites are keyed by the name without it.
	written := strings.TrimSuffix(short, "!")
	postings := [][]uint32{resolver.snapshot.Query.ShortOrdinals(written)}
	if written != short {
		postings = append(postings, resolver.snapshot.Query.ShortOrdinals(short))
	}
	seen := make(map[uint32]struct{}, 16)
	for _, posting := range postings {
		for _, ordinal := range posting {
			reference, ok := resolver.visit(ordinal)
			if !ok {
				return
			}
			if reference.RecordKind != model.Reference {
				continue
			}
			if _, exists := seen[ordinal]; exists {
				continue
			}
			seen[ordinal] = struct{}{}
			resolver.callerEdge(collector, anchor, reference, written)
		}
	}
}

func (resolver *relatedResolver) callerEdge(collector *relatedCollector, anchor uint32, reference model.Record, written string) {
	entries, ok := model.ParseReferenceTable(reference.TargetName)
	if !ok {
		return
	}
	best := model.ReferenceEntry{}
	evidence := model.EvidenceClass("")
	for _, entry := range entries {
		if lastNameSegment(normalize(entry.Name)) != written {
			continue
		}
		resolution := resolver.resolveTarget(reference.Path, reference.QualifiedName, entry.Name)
		if !resolution.resolved || !slices.Contains(resolution.candidates, anchor) {
			continue
		}
		candidate := relatedEdge{evidence: resolution.evidence, line: entry.Line}
		if evidence == "" || betterRelatedEdge(candidate, relatedEdge{evidence: evidence, line: best.Line}) {
			best, evidence = entry, resolution.evidence
		}
	}
	if evidence == "" {
		return
	}
	collector.add(relatedEdge{
		record: resolver.callerRecord(reference, best.Line), relation: relationCall,
		evidence: evidence, line: best.Line, count: best.Count,
	})
}

// callerRecord is the record a reference belongs to: the enclosing definition
// or, in Go, the package record. A module-level use in a tree-sitter language
// has no record of its own, so the reference itself is reshaped into the
// module host the design describes; source_snippets refuses that identity, and
// the path and line stay usable.
func (resolver *relatedResolver) callerRecord(reference model.Record, line int) model.Record {
	view := resolver.file(reference.Path)
	for _, ordinal := range view.definitions {
		record, ok := resolver.visit(ordinal)
		if !ok {
			break
		}
		if record.QualifiedName == reference.QualifiedName {
			return record
		}
	}
	if view.module >= 0 {
		if record, ok := resolver.visit(uint32(view.module)); ok && record.QualifiedName == reference.QualifiedName {
			return record
		}
	}
	host := reference
	host.RecordKind = model.Module
	host.StartLine, host.EndLine = line, line
	host.Preview = ""
	host.TargetName, host.ReferenceCount, host.SearchTerms = "", 0, nil
	return host
}

// callees reads the anchor's own reference record and resolves every name it
// uses from the anchor's file.
func (resolver *relatedResolver) callees(collector *relatedCollector, anchor model.Record) {
	view := resolver.file(anchor.Path)
	for _, ordinal := range view.references {
		reference, ok := resolver.visit(ordinal)
		if !ok {
			return
		}
		if reference.QualifiedName != anchor.QualifiedName {
			continue
		}
		entries, parsed := model.ParseReferenceTable(reference.TargetName)
		if !parsed {
			continue
		}
		for _, entry := range entries {
			resolution := resolver.resolveTarget(anchor.Path, reference.QualifiedName, entry.Name)
			if !resolution.resolved {
				continue
			}
			for _, candidate := range resolution.candidates {
				record, visited := resolver.visit(candidate)
				if !visited {
					return
				}
				collector.add(relatedEdge{
					record: record, relation: relationCall, evidence: resolution.evidence,
					line: entry.Line, count: entry.Count,
				})
			}
		}
	}
}

// importers scans the import records for the ones that name the anchor's
// module. The findings are those import records themselves: they are real,
// snippet-able records that show the importing file and line.
func (resolver *relatedResolver) importers(collector *relatedCollector, anchor uint32, record model.Record) {
	module := resolver.anchorModuleName(record)
	if module == "" {
		return
	}
	isModule := record.RecordKind == model.Module
	unique := false
	if isModule {
		// The claim is verified when the imported module name resolves to the
		// anchor's own package and to no other.
		if hosted, ok := resolver.moduleNamed(module, record.Language); ok {
			if hostRecord, visited := resolver.visit(uint32(hosted)); visited {
				unique = pathDirectory(hostRecord.Path) == pathDirectory(record.Path)
			}
		}
	}
	short := lastNameSegment(normalize(record.QualifiedName))
	for _, ordinal := range resolver.snapshot.Query.FacetOrdinals(store.QueryFacetKind, string(model.Import)) {
		imported, ok := resolver.visit(ordinal)
		if !ok {
			return
		}
		if imported.RecordKind != model.Import || importedModuleName(imported) != module {
			continue
		}
		// An import written in another language names another module that
		// happens to be spelled the same way.
		if imported.Language != record.Language {
			continue
		}
		bound := boundImportName(imported)
		evidence := model.Inferred
		switch {
		case isModule:
			if unique {
				evidence = model.Verified
			}
		case bound == wildcardBinding:
			// A wildcard binds every name of the module, so the file may or
			// may not use this one.
		case bound != short:
			continue
		default:
			// An import sits at the file's module level, so nothing nested is
			// visible to the name it binds.
			resolution := resolver.resolveTarget(imported.Path, "", bound)
			if !resolution.resolved || !slices.Contains(resolution.candidates, anchor) {
				continue
			}
			evidence = resolution.evidence
		}
		collector.add(relatedEdge{
			record: imported, relation: relationImport, evidence: evidence,
			line: imported.StartLine, count: 1,
		})
	}
}

// imports resolves the import records of the anchor's file to what they bring
// in: the definition a single-name import binds, or the module record itself
// where the index has one.
func (resolver *relatedResolver) imports(collector *relatedCollector, anchor model.Record) {
	view := resolver.file(anchor.Path)
	for _, ordinal := range view.imports {
		imported, ok := resolver.visit(ordinal)
		if !ok {
			return
		}
		module := importedModuleName(imported)
		if module == "" {
			continue
		}
		bound := boundImportName(imported)
		if bound != wildcardBinding && bound != module {
			// A neighbouring specifier names the module of this file's own
			// directory, exactly as rule 2 reads it, so the same import
			// carries the same evidence whichever direction it is asked from.
			directory := importDirectory(imported)
			named, complete := resolver.definitionsNamed(bound, bound, view.language)
			candidates := resolver.restrictDefinitions(named, module, directory)
			if len(candidates) == 0 && directory != "" {
				candidates = resolver.restrictDefinitions(named, module, "")
			}
			if len(candidates) != 0 {
				evidence := model.Inferred
				if len(candidates) == 1 && complete && view.complete {
					evidence = model.Verified
				} else {
					resolver.sortByPath(candidates)
					if len(candidates) > maximumInferredCandidates {
						candidates = candidates[:maximumInferredCandidates]
					}
				}
				for _, candidate := range candidates {
					record, visited := resolver.visit(candidate)
					if !visited {
						return
					}
					collector.add(relatedEdge{
						record: record, relation: relationImport, evidence: evidence,
						line: imported.StartLine, count: 1,
					})
				}
				continue
			}
		}
		if hosted, ok := resolver.moduleNamed(module, view.language); ok {
			record, visited := resolver.visit(uint32(hosted))
			if !visited {
				return
			}
			collector.add(relatedEdge{
				record: record, relation: relationImport, evidence: model.Verified,
				line: imported.StartLine, count: 1,
			})
		}
	}
}

// anchorModuleName is the module an anchor belongs to: its own name for a
// module record, the module record of its file where the language has one, and
// otherwise the first segment of its qualified name, which every extractor
// builds from the module's own name.
func (resolver *relatedResolver) anchorModuleName(anchor model.Record) string {
	if anchor.RecordKind == model.Module {
		return normalize(lastNameSegment(anchor.QualifiedName))
	}
	if view := resolver.file(anchor.Path); view.module >= 0 {
		if record, ok := resolver.visit(uint32(view.module)); ok {
			return normalize(lastNameSegment(record.QualifiedName))
		}
	}
	return qualifyingModuleName(normalize(anchor.QualifiedName))
}

// recordModuleName is the module a record belongs to, read from its qualified
// name alone so it works in the four languages that emit no module record. A
// bare name carries no module.
func recordModuleName(record model.Record) string {
	if record.RecordKind == model.Module {
		return normalize(lastNameSegment(record.QualifiedName))
	}
	return qualifyingModuleName(normalize(record.QualifiedName))
}

// matchesTargetName reports whether a definition can be what a written name
// meant: the last segments agree - a Rust macro definition keeping the "!" a
// call site omits - and a dotted name must also be a suffix of the qualified
// name, so "osp.join" never matches an unrelated "join".
func matchesTargetName(record model.Record, target, short string) bool {
	name := normalize(record.QualifiedName)
	last := lastNameSegment(name)
	if last != short && last != short+"!" {
		return false
	}
	normalized := normalize(target)
	if strings.Contains(normalized, ".") {
		return name == normalized || strings.HasSuffix(name, "."+normalized)
	}
	return true
}

// importedModuleName is the module name an import specifier names. Module
// systems spell a specifier differently, so the tail is taken the way the
// language writes it: a path segment, a dotted package tail, or, in Rust, the
// segment in front of the item the use path ends with.
func importedModuleName(record model.Record) string {
	specifier := normalize(record.TargetName)
	if specifier == "" {
		return ""
	}
	if index := strings.LastIndexByte(specifier, '/'); index >= 0 {
		specifier = specifier[index+1:]
	}
	switch record.Language {
	case "javascript", "typescript":
		if index := strings.IndexByte(specifier, '.'); index > 0 {
			specifier = specifier[:index]
		}
	case "rust":
		segments := strings.Split(specifier, ".")
		if len(segments) > 1 && segments[len(segments)-1] == boundImportName(record) {
			segments = segments[:len(segments)-1]
		}
		specifier = segments[len(segments)-1]
	default:
		specifier = lastNameSegment(specifier)
	}
	return specifier
}

// boundImportName is the local name an import binds, which is the last segment
// of the record's qualified name in every language the engine reads.
func boundImportName(record model.Record) string {
	name := normalize(record.QualifiedName)
	if index := strings.LastIndex(name, "::"); index >= 0 {
		name = name[index+2:]
	}
	return lastNameSegment(name)
}

func lastNameSegment(value string) string {
	if index := strings.LastIndexByte(value, '.'); index >= 0 {
		return value[index+1:]
	}
	return value
}

// firstNameSegment is the qualifier a written name starts with, and the whole
// name when it carries none.
func firstNameSegment(value string) string {
	if index := strings.IndexByte(value, '.'); index > 0 {
		return value[:index]
	}
	return value
}

// qualifyingModuleName is the module part of a qualified name, and is empty
// for a bare name, which belongs to no module the index can name.
func qualifyingModuleName(value string) string {
	index := strings.IndexByte(value, '.')
	if index <= 0 {
		return ""
	}
	return value[:index]
}
