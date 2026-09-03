package extract

import (
	"context"
	"errors"
	"path"
	"sort"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	sitter "github.com/tree-sitter/go-tree-sitter"
)

const (
	maximumTreeSitterDepth       = 64
	maximumTreeSitterCaptures    = 8192
	maximumTreeSitterRecords     = 4096
	maximumTreeSitterImportNodes = 4096
	treeSitterQueryMatchLimit    = 4096
	maximumDottedTargetDepth     = 32
	maximumTargetSpecifierBytes  = 512
)

var (
	errTreeSitterParse     = errors.New("tree-sitter parse failed")
	errTreeSitterCancelled = errors.New("tree-sitter parse cancelled")
)

type treeSitterGrammar struct {
	language      *sitter.Language
	query         string
	parserVersion string
	warningPrefix string
	handle        func(*treeSitterAnalysis, *sitter.Node)
}

type treeSitterAnalysis struct {
	ctx           context.Context
	source        []byte
	module        string
	lineStarts    []int
	records       []model.Record
	warnings      []string
	parseFailures int
	stopped       bool
	// references groups the uses of names by the definition that contains
	// them, so the file contributes at most one reference record per
	// definition.
	references referenceCollector
}

func extractTreeSitter(ctx context.Context, file boundary.StableFile, grammar treeSitterGrammar) ([]model.Record, Report) {
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
	}
	tree, err := parseTree(ctx, grammar.language, file.Bytes)
	if err != nil {
		warning := grammar.warningPrefix + "-parse-failure"
		if errors.Is(err, errTreeSitterCancelled) {
			warning = "tree-sitter-cancelled"
		}
		return nil, treeSitterFailure(grammar.parserVersion, warning)
	}
	defer tree.Close()

	query, queryError := sitter.NewQuery(grammar.language, grammar.query)
	if queryError != nil || query == nil {
		return nil, treeSitterFailure(grammar.parserVersion, grammar.warningPrefix+"-query-failure")
	}
	defer query.Close()
	if ctx.Err() != nil {
		return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
	}

	cursor := sitter.NewQueryCursor()
	if cursor == nil {
		return nil, treeSitterFailure(grammar.parserVersion, grammar.warningPrefix+"-query-failure")
	}
	defer cursor.Close()
	cursor.SetMatchLimit(treeSitterQueryMatchLimit)

	root := tree.RootNode()
	if root == nil {
		return nil, treeSitterFailure(grammar.parserVersion, grammar.warningPrefix+"-parse-failure")
	}
	analysis := &treeSitterAnalysis{
		ctx:        ctx,
		source:     file.Bytes,
		module:     sourceModule(file.RelativePath),
		lineStarts: sourceLineStarts(file.Bytes),
		records:    make([]model.Record, 0, 128),
	}
	if root.HasError() || root.IsError() || root.IsMissing() {
		analysis.parseFailures = 1
		analysis.addWarning(grammar.warningPrefix + "-syntax-error")
	}

	matches := cursor.MatchesWithOptions(query, root, file.Bytes, sitter.QueryCursorOptions{
		ProgressCallback: func(sitter.QueryCursorState) bool {
			return ctx.Err() != nil
		},
	})
	if ctx.Err() != nil {
		return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
	}
	captures := 0
	for !analysis.stopped {
		if ctx.Err() != nil {
			return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
		}
		match := matches.Next()
		if ctx.Err() != nil {
			return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
		}
		if match == nil {
			break
		}
		for captureIndex := range match.Captures {
			captures++
			if captures > maximumTreeSitterCaptures {
				analysis.limit("tree-sitter-capture-limit")
				break
			}
			node := &match.Captures[captureIndex].Node
			usable, tooDeep := analysis.nodeUsable(node)
			if tooDeep {
				analysis.limit("tree-sitter-depth-limit")
				continue
			}
			if !usable {
				continue
			}
			grammar.handle(analysis, node)
			if analysis.stopped {
				break
			}
		}
	}
	if cursor.DidExceedMatchLimit() {
		analysis.limit("tree-sitter-match-limit")
	}
	analysis.flushReferences()
	if ctx.Err() != nil {
		return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
	}
	sort.Strings(analysis.warnings)
	if ctx.Err() != nil {
		return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
	}
	return analysis.records, Report{
		ParserVersion: grammar.parserVersion,
		ParseFailures: analysis.parseFailures,
		WarningCodes:  analysis.warnings,
	}
}

// parseTree owns and closes a fresh parser for every call. The returned tree
// belongs to the caller. Cancellation is checked by the binding-owned native
// progress callback; C never retains a pointer into Go memory between calls.
func parseTree(ctx context.Context, language *sitter.Language, source []byte) (*sitter.Tree, error) {
	if language == nil {
		return nil, errTreeSitterParse
	}
	if err := ctx.Err(); err != nil {
		return nil, errTreeSitterCancelled
	}
	parser := sitter.NewParser()
	if parser == nil {
		return nil, errTreeSitterParse
	}
	defer parser.Close()
	if err := parser.SetLanguage(language); err != nil {
		return nil, errTreeSitterParse
	}
	length := len(source)
	tree := parser.ParseWithOptions(func(offset int, _ sitter.Point) []byte {
		if offset >= 0 && offset < length {
			return source[offset:]
		}
		return nil
	}, nil, &sitter.ParseOptions{
		ProgressCallback: func(sitter.ParseState) bool {
			return ctx.Err() != nil
		},
	})
	if tree == nil {
		if ctx.Err() != nil {
			return nil, errTreeSitterCancelled
		}
		return nil, errTreeSitterParse
	}
	if ctx.Err() != nil {
		tree.Close()
		return nil, errTreeSitterCancelled
	}
	return tree, nil
}

func treeSitterFailure(parserVersion, warning string) Report {
	return Report{ParserVersion: parserVersion, ParseFailures: 1, WarningCodes: []string{warning}}
}

func sourceModule(relative string) string {
	base := path.Base(relative)
	base = strings.TrimSuffix(base, path.Ext(base))
	if base == "__init__" {
		base = path.Base(path.Dir(relative))
	}
	return base
}

func sourceLineStarts(source []byte) []int {
	starts := make([]int, 1, 1+len(source)/32)
	for index, value := range source {
		if value == '\n' && index+1 < len(source) {
			starts = append(starts, index+1)
		}
	}
	return starts
}

func (analysis *treeSitterAnalysis) nodeLines(node *sitter.Node) (int, int, bool) {
	if node == nil {
		return 0, 0, false
	}
	start, end := node.StartByte(), node.EndByte()
	if start >= end || end > uint(len(analysis.source)) || start > uint(len(analysis.source)) {
		analysis.limit("tree-sitter-invalid-range")
		return 0, 0, false
	}
	startOffset, endOffset := int(start), int(end-1)
	startLine := sort.Search(len(analysis.lineStarts), func(index int) bool {
		return analysis.lineStarts[index] > startOffset
	})
	endLine := sort.Search(len(analysis.lineStarts), func(index int) bool {
		return analysis.lineStarts[index] > endOffset
	})
	if startLine < 1 || endLine < startLine {
		analysis.limit("tree-sitter-invalid-range")
		return 0, 0, false
	}
	return startLine, endLine, true
}

func (analysis *treeSitterAnalysis) nodeText(node *sitter.Node) (string, bool) {
	if node == nil {
		return "", false
	}
	start, end := node.StartByte(), node.EndByte()
	if start >= end || end > uint(len(analysis.source)) || start > uint(len(analysis.source)) {
		analysis.limit("tree-sitter-invalid-range")
		return "", false
	}
	return string(analysis.source[int(start):int(end)]), true
}

func (analysis *treeSitterAnalysis) nodeUsable(node *sitter.Node) (usable, tooDeep bool) {
	if node == nil || node.IsError() || node.IsMissing() || node.HasError() {
		return false, false
	}
	depth := 0
	for current := node; current != nil; current = current.Parent() {
		depth++
		if depth > maximumTreeSitterDepth {
			return false, true
		}
		parent := current.Parent()
		if parent == nil {
			break
		}
		// The root can contain an unrelated sibling error. Any lower erroneous
		// container overlaps the candidate and therefore cannot support verified evidence.
		if parent.Parent() != nil && (parent.IsError() || parent.IsMissing() || parent.HasError()) {
			return false, false
		}
	}
	_, _, validRange := analysis.nodeLines(node)
	return validRange, false
}

func (analysis *treeSitterAnalysis) appendNodeRecord(node *sitter.Node, qualified string, kind model.RecordKind, evidence model.EvidenceClass) {
	if analysis.stopped || qualified == "" {
		return
	}
	if len(analysis.records) >= maximumTreeSitterRecords {
		analysis.limit("tree-sitter-record-limit")
		return
	}
	start, end, ok := analysis.nodeLines(node)
	if !ok {
		return
	}
	analysis.records = append(analysis.records, model.Record{
		StartLine:     start,
		EndLine:       end,
		RecordKind:    kind,
		SourceType:    "source",
		QualifiedName: qualified,
		EvidenceClass: evidence,
	})
}

// appendImportRecord records one bound local name of an import statement
// together with the module specifier it was imported from. An unusable
// specifier only leaves the target empty; the binding itself stays indexed.
func (analysis *treeSitterAnalysis) appendImportRecord(node *sitter.Node, binding, target string) {
	if target != "" && (len(target) > maximumTargetSpecifierBytes || strings.ContainsAny(target, "\x00\n\r")) {
		target = ""
	}
	before := len(analysis.records)
	analysis.appendNodeRecord(node, binding, model.Import, model.Verified)
	if len(analysis.records) > before {
		analysis.records[len(analysis.records)-1].TargetName = target
	}
}

// appendReference merges one use of target inside the enclosing definition
// into that definition's reference record. Targets that are not stable written
// names are counted as skipped instead of recorded.
func (analysis *treeSitterAnalysis) appendReference(node *sitter.Node, scope referenceScope, target string) {
	if analysis.stopped {
		return
	}
	line := 0
	if model.ValidReferenceTargetName(target) {
		start, _, ok := analysis.nodeLines(node)
		if !ok {
			return
		}
		line = start
	}
	analysis.references.add(scope, target, line)
}

// flushReferences appends the grouped reference records after every structural
// record of the file, so the per-file record limit drops references before it
// drops a definition.
func (analysis *treeSitterAnalysis) flushReferences() {
	records, warnings := analysis.references.flush()
	for index := range records {
		if len(analysis.records) >= maximumTreeSitterRecords {
			analysis.limit("tree-sitter-record-limit")
			break
		}
		analysis.records = append(analysis.records, records[index])
	}
	for _, warning := range warnings {
		analysis.addWarning(warning)
	}
}

// enclosingScope is the definition that lexically contains node: its qualified
// name exactly as its own definition record carries it, and its line range. At
// file scope it is the module's name and the whole file, which is the module's
// range. rangeOf promotes the scope node to the node whose range the
// definition record uses (a decorated definition, for example); it may be nil.
func (analysis *treeSitterAnalysis) enclosingScope(node *sitter.Node, scope func(*sitter.Node) (string, bool), rangeOf func(*sitter.Node) *sitter.Node) (referenceScope, bool) {
	prefix, innermost, ok := analysis.lexicalScope(node, scope)
	if !ok {
		return referenceScope{}, false
	}
	enclosing := referenceScope{name: analysis.qualified(prefix...), start: 1, end: len(analysis.lineStarts)}
	if innermost == nil {
		return enclosing, enclosing.end >= enclosing.start
	}
	if rangeOf != nil {
		if promoted := rangeOf(innermost); promoted != nil {
			innermost = promoted
		}
	}
	start, end, ok := analysis.nodeLines(innermost)
	if !ok {
		return referenceScope{}, false
	}
	enclosing.start, enclosing.end = start, end
	return enclosing, true
}

// dottedTarget renders a call target as a dotted name. leaf names the node
// kinds that are one component on their own; dotted names the kinds that join
// an object field and a property field with a dot. Anything else - a computed
// member, a call returning a callable, a generated name - has no stable
// written name and is reported as unusable.
type dottedTargetRules struct {
	leaf       []string
	containers []dottedContainer
}

// dottedContainer is one grammar node that joins a qualifying expression with
// the name it selects, named by their field names in that grammar.
type dottedContainer struct {
	kind     string
	object   string
	property string
}

func (analysis *treeSitterAnalysis) dottedTarget(node *sitter.Node, rules dottedTargetRules, depth int) (string, bool) {
	if node == nil || depth > maximumDottedTargetDepth {
		return "", false
	}
	if name, ok := analysis.stableName(node, rules.leaf...); ok {
		return name, true
	}
	for _, container := range rules.containers {
		if node.Kind() != container.kind {
			continue
		}
		object, objectOK := analysis.dottedTarget(node.ChildByFieldName(container.object), rules, depth+1)
		if !objectOK {
			return "", false
		}
		property, propertyOK := analysis.stableName(node.ChildByFieldName(container.property), rules.leaf...)
		if !propertyOK {
			return "", false
		}
		return object + "." + property, true
	}
	return "", false
}

func (analysis *treeSitterAnalysis) qualified(parts ...string) string {
	all := make([]string, 0, len(parts)+1)
	if analysis.module != "" {
		all = append(all, analysis.module)
	}
	for _, part := range parts {
		if part != "" {
			all = append(all, part)
		}
	}
	return strings.Join(all, ".")
}

func (analysis *treeSitterAnalysis) lexicalPrefix(node *sitter.Node, scope func(*sitter.Node) (string, bool)) ([]string, bool) {
	prefix, _, ok := analysis.lexicalScope(node, scope)
	return prefix, ok
}

// lexicalScope returns the names of the definitions that lexically contain
// node, outermost first, together with the innermost of those nodes (nil at
// file scope).
func (analysis *treeSitterAnalysis) lexicalScope(node *sitter.Node, scope func(*sitter.Node) (string, bool)) ([]string, *sitter.Node, bool) {
	var reversed []string
	var innermost *sitter.Node
	depth := 0
	for parent := node.Parent(); parent != nil; parent = parent.Parent() {
		depth++
		if depth > maximumTreeSitterDepth {
			analysis.limit("tree-sitter-depth-limit")
			return nil, nil, false
		}
		if name, ok := scope(parent); ok {
			reversed = append(reversed, name)
			if innermost == nil {
				innermost = parent
			}
		}
	}
	for left, right := 0, len(reversed)-1; left < right; left, right = left+1, right-1 {
		reversed[left], reversed[right] = reversed[right], reversed[left]
	}
	return reversed, innermost, true
}

func (analysis *treeSitterAnalysis) stableName(node *sitter.Node, kinds ...string) (string, bool) {
	if node == nil || node.IsError() || node.IsMissing() || node.HasError() {
		return "", false
	}
	wanted := false
	for _, kind := range kinds {
		if node.Kind() == kind {
			wanted = true
			break
		}
	}
	if !wanted {
		return "", false
	}
	text, ok := analysis.nodeText(node)
	if !ok || text == "" || len(text) > 256 || strings.ContainsAny(text, "\x00\n\r") {
		return "", false
	}
	return text, true
}

func (analysis *treeSitterAnalysis) addWarning(warning string) {
	if warning != "" {
		analysis.warnings = append(analysis.warnings, warning)
	}
}

func (analysis *treeSitterAnalysis) limit(warning string) {
	analysis.addWarning(warning)
	analysis.parseFailures = 1
	if warning == "tree-sitter-record-limit" || warning == "tree-sitter-capture-limit" || warning == "tree-sitter-match-limit" {
		analysis.stopped = true
	}
}

func (analysis *treeSitterAnalysis) childByKind(node *sitter.Node, kind string) *sitter.Node {
	if node == nil {
		return nil
	}
	count := analysis.boundedNamedChildCount(node, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
	for index := uint(0); index < count; index++ {
		child := node.NamedChild(index)
		if child != nil && child.Kind() == kind {
			return child
		}
	}
	return nil
}

func (analysis *treeSitterAnalysis) boundedNamedChildCount(node *sitter.Node, maximum uint, warning string) uint {
	if node == nil {
		return 0
	}
	count := node.NamedChildCount()
	if count > maximum {
		analysis.limit(warning)
		return maximum
	}
	return count
}

func unquotedString(text string) (string, bool) {
	if len(text) < 2 || (text[0] != '\'' && text[0] != '"') || text[len(text)-1] != text[0] || strings.Contains(text[1:len(text)-1], "\\") {
		return "", false
	}
	return text[1 : len(text)-1], true
}
