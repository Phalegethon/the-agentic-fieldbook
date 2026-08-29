package extract

import (
	"context"
	"errors"
	"path"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

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
	ambiguous     bool
	stopped       bool
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

	matches := cursor.Matches(query, root, file.Bytes)
	captures := 0
	for !analysis.stopped {
		if ctx.Err() != nil {
			return nil, treeSitterFailure(grammar.parserVersion, "tree-sitter-cancelled")
		}
		match := matches.Next()
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
	if analysis.ambiguous {
		for index := range analysis.records {
			if analysis.records[index].EvidenceClass == model.Verified {
				analysis.records[index].EvidenceClass = model.Inferred
			}
		}
	}
	sort.Strings(analysis.warnings)
	return analysis.records, Report{
		ParserVersion: grammar.parserVersion,
		ParseFailures: analysis.parseFailures,
		WarningCodes:  analysis.warnings,
	}
}

// parseTree owns and closes a fresh parser for every call. The returned tree
// belongs to the caller. A caller-owned atomic flag keeps cancellation memory
// valid until the watcher has stopped and prevents parser reuse races.
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
	var cancellation uintptr
	parser.SetCancellationFlag(&cancellation)
	done := make(chan struct{})
	var watcher sync.WaitGroup
	watcher.Add(1)
	go func() {
		defer watcher.Done()
		select {
		case <-ctx.Done():
			atomic.StoreUintptr(&cancellation, 1)
		case <-done:
		}
	}()
	tree := parser.Parse(source, nil)
	close(done)
	watcher.Wait()
	parser.SetCancellationFlag(nil)
	if tree == nil {
		if ctx.Err() != nil || atomic.LoadUintptr(&cancellation) != 0 {
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
	var reversed []string
	depth := 0
	for parent := node.Parent(); parent != nil; parent = parent.Parent() {
		depth++
		if depth > maximumTreeSitterDepth {
			analysis.limit("tree-sitter-depth-limit")
			return nil, false
		}
		if name, ok := scope(parent); ok {
			reversed = append(reversed, name)
		}
	}
	for left, right := 0, len(reversed)-1; left < right; left, right = left+1, right-1 {
		reversed[left], reversed[right] = reversed[right], reversed[left]
	}
	return reversed, true
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
	analysis.ambiguous = true
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
