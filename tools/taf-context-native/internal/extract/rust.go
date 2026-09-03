package extract

import (
	"context"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	sitter "github.com/tree-sitter/go-tree-sitter"
	rust "github.com/tree-sitter/tree-sitter-rust/bindings/go"
)

const rustParserVersion = "tree-sitter-rust@0.24.2"

const rustQuery = `
[
  (struct_item)
  (enum_item)
  (trait_item)
  (function_item)
  (function_signature_item)
  (mod_item)
  (use_declaration)
  (macro_definition)
  (macro_invocation)
  (call_expression)
] @item
`

type rustExtractor struct{ extensions []string }

func (extractor rustExtractor) Language() string      { return "rust" }
func (extractor rustExtractor) ParserVersion() string { return rustParserVersion }
func (extractor rustExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor rustExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}
func (extractor rustExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	return extractor.ExtractContext(context.Background(), file)
}
func (extractor rustExtractor) ExtractContext(ctx context.Context, file boundary.StableFile) ([]model.Record, Report) {
	return extractTreeSitter(ctx, file, treeSitterGrammar{
		language:      sitter.NewLanguage(rust.Language()),
		query:         rustQuery,
		parserVersion: rustParserVersion,
		warningPrefix: "rust",
		handle:        handleRustNode,
	})
}

func handleRustNode(analysis *treeSitterAnalysis, node *sitter.Node) {
	switch node.Kind() {
	case "struct_item", "enum_item", "trait_item", "mod_item", "macro_definition":
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier", "type_identifier")
		if !ok {
			analysis.addWarning("rust-generated-name")
			return
		}
		prefix, ok := rustLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "function_item", "function_signature_item":
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier")
		if !ok {
			analysis.addWarning("rust-generated-name")
			return
		}
		prefix, ok := rustLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "use_declaration":
		argument := node.ChildByFieldName("argument")
		for _, binding := range rustUseBindings(analysis, argument) {
			analysis.appendImportRecord(node, binding.name, binding.target)
		}
	case "macro_invocation":
		macro, ok := analysis.nodeText(node.ChildByFieldName("macro"))
		if !ok || macro == "" || len(macro) > 256 || strings.ContainsAny(macro, "\x00\n\r") {
			analysis.addWarning("rust-generated-name")
			return
		}
		prefix, ok := rustLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, macro+"!")...), model.Definition, model.Inferred)
		// The expanded macro is invisible here, so the invocation is recorded
		// as a use of the macro name itself.
		analysis.appendReference(node, analysis.qualified(prefix...), dottedRustPath(macro))
	case "call_expression":
		enclosing, ok := analysis.enclosingName(node, rustScope(analysis))
		if !ok {
			return
		}
		target, _ := analysis.dottedTarget(node.ChildByFieldName("function"), rustTargetRules, 0)
		analysis.appendReference(node, enclosing, target)
	}
}

var rustTargetRules = dottedTargetRules{
	leaf: []string{"identifier", "field_identifier", "type_identifier", "crate", "self", "super"},
	containers: []dottedContainer{
		{kind: "scoped_identifier", object: "path", property: "name"},
		{kind: "field_expression", object: "value", property: "field"},
	},
}

// dottedRustPath renders a Rust path in the dotted form every language uses
// for reference and import targets.
func dottedRustPath(value string) string {
	return strings.ReplaceAll(value, "::", ".")
}

func rustLexicalPrefix(analysis *treeSitterAnalysis, node *sitter.Node) ([]string, bool) {
	return analysis.lexicalPrefix(node, rustScope(analysis))
}

// rustScope names the items that lexically contain a node: modules, traits,
// functions, and the type an impl block applies to.
func rustScope(analysis *treeSitterAnalysis) func(*sitter.Node) (string, bool) {
	return func(parent *sitter.Node) (string, bool) {
		switch parent.Kind() {
		case "mod_item", "trait_item", "function_item", "function_signature_item":
			return analysis.stableName(parent.ChildByFieldName("name"), "identifier", "type_identifier")
		case "impl_item":
			typeNode := parent.ChildByFieldName("type")
			name, ok := analysis.nodeText(typeNode)
			if !ok || name == "" || len(name) > 256 || strings.ContainsAny(name, "\x00\n\r") {
				return "", false
			}
			return name, true
		}
		return "", false
	}
}

type rustUseWork struct {
	node   *sitter.Node
	prefix string
	depth  int
}

func rustUseBindings(analysis *treeSitterAnalysis, root *sitter.Node) []importBinding {
	if root == nil {
		return nil
	}
	work := []rustUseWork{{node: root, depth: 1}}
	bindings := make([]importBinding, 0, 4)
	visits := 0
	for len(work) > 0 {
		last := len(work) - 1
		item := work[last]
		work = work[:last]
		if item.depth > maximumTreeSitterDepth {
			analysis.limit("tree-sitter-depth-limit")
			continue
		}
		if visits >= maximumTreeSitterImportNodes {
			analysis.limit("tree-sitter-import-limit")
			break
		}
		visits++
		switch item.node.Kind() {
		case "use_as_clause":
			if alias, ok := analysis.stableName(item.node.ChildByFieldName("alias"), "identifier"); ok {
				target := ""
				if pathText, pathOK := analysis.nodeText(item.node.ChildByFieldName("path")); pathOK {
					target = dottedRustPath(joinRustPath(item.prefix, pathText))
				}
				bindings = append(bindings, importBinding{name: alias, target: target})
			}
		case "scoped_use_list":
			pathText, ok := analysis.nodeText(item.node.ChildByFieldName("path"))
			list := item.node.ChildByFieldName("list")
			if ok && list != nil {
				work = append(work, rustUseWork{node: list, prefix: joinRustPath(item.prefix, pathText), depth: item.depth + 1})
			}
		case "use_list":
			childCount := int(item.node.NamedChildCount())
			available := maximumTreeSitterImportNodes - visits - len(work)
			if childCount > available {
				childCount = available
				analysis.limit("tree-sitter-import-limit")
			}
			for index := childCount - 1; index >= 0; index-- {
				if child := item.node.NamedChild(uint(index)); child != nil {
					work = append(work, rustUseWork{node: child, prefix: item.prefix, depth: item.depth + 1})
				}
			}
		case "scoped_identifier", "identifier", "crate", "self", "super", "use_wildcard":
			name, ok := analysis.nodeText(item.node)
			if ok && name != "" && !strings.ContainsAny(name, "\x00\n\r") {
				full := joinRustPath(item.prefix, name)
				bindings = append(bindings, importBinding{name: full, target: dottedRustPath(full)})
			}
		case "metavariable":
			analysis.addWarning("rust-generated-name")
		}
	}
	return bindings
}

func joinRustPath(prefix, name string) string {
	if prefix == "" {
		return name
	}
	if name == "" {
		return prefix
	}
	return prefix + "::" + name
}
