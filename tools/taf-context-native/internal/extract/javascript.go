package extract

import (
	"context"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	sitter "github.com/tree-sitter/go-tree-sitter"
	javascript "github.com/tree-sitter/tree-sitter-javascript/bindings/go"
)

const javascriptParserVersion = "tree-sitter-javascript@0.25.0"

const javascriptQuery = `
[
  (class_declaration)
  (function_declaration)
  (method_definition)
  (variable_declarator)
  (import_statement)
  (call_expression)
] @item
`

type javascriptExtractor struct{ extensions []string }

func (extractor javascriptExtractor) Language() string      { return "javascript" }
func (extractor javascriptExtractor) ParserVersion() string { return javascriptParserVersion }
func (extractor javascriptExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor javascriptExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}
func (extractor javascriptExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	return extractor.ExtractContext(context.Background(), file)
}
func (extractor javascriptExtractor) ExtractContext(ctx context.Context, file boundary.StableFile) ([]model.Record, Report) {
	return extractTreeSitter(ctx, file, treeSitterGrammar{
		language:      sitter.NewLanguage(javascript.Language()),
		query:         javascriptQuery,
		parserVersion: javascriptParserVersion,
		warningPrefix: "javascript",
		handle: func(analysis *treeSitterAnalysis, node *sitter.Node) {
			handleECMAScriptNode(analysis, node, false, "javascript")
		},
	})
}

func handleECMAScriptNode(analysis *treeSitterAnalysis, node *sitter.Node, typescript bool, warningPrefix string) {
	switch node.Kind() {
	case "class_declaration", "function_declaration", "interface_declaration", "type_alias_declaration":
		if !typescript && (node.Kind() == "interface_declaration" || node.Kind() == "type_alias_declaration") {
			return
		}
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier", "type_identifier")
		if !ok {
			analysis.addWarning(warningPrefix + "-generated-name")
			return
		}
		prefix, ok := ecmaLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "method_definition":
		parent := node.Parent()
		if parent == nil || parent.Kind() != "class_body" {
			return
		}
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "property_identifier", "private_property_identifier", "identifier")
		if !ok {
			analysis.addWarning(warningPrefix + "-generated-name")
			return
		}
		prefix, ok := ecmaLexicalPrefix(analysis, node)
		if !ok || len(prefix) == 0 {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "variable_declarator":
		value := node.ChildByFieldName("value")
		if value == nil || value.Kind() != "arrow_function" {
			return
		}
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier")
		if !ok {
			analysis.addWarning(warningPrefix + "-generated-name")
			return
		}
		prefix, ok := ecmaLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "import_statement":
		for _, binding := range ecmaImportBindings(analysis, node) {
			analysis.appendNodeRecord(node, binding, model.Import, model.Verified)
		}
	case "call_expression":
		if ecmaDynamicLookup(analysis, node) {
			analysis.ambiguous = true
			analysis.addWarning(warningPrefix + "-dynamic-lookup")
		}
	}
}

func ecmaLexicalPrefix(analysis *treeSitterAnalysis, node *sitter.Node) ([]string, bool) {
	return analysis.lexicalPrefix(node, func(parent *sitter.Node) (string, bool) {
		switch parent.Kind() {
		case "class_declaration", "function_declaration", "method_definition":
			return analysis.stableName(parent.ChildByFieldName("name"), "identifier", "type_identifier", "property_identifier", "private_property_identifier")
		case "variable_declarator":
			value := parent.ChildByFieldName("value")
			if value != nil && value.Kind() == "arrow_function" {
				return analysis.stableName(parent.ChildByFieldName("name"), "identifier")
			}
		}
		return "", false
	})
}

func ecmaImportBindings(analysis *treeSitterAnalysis, node *sitter.Node) []string {
	clause := analysis.childByKind(node, "import_clause")
	if clause == nil {
		source := node.ChildByFieldName("source")
		text, ok := analysis.nodeText(source)
		if !ok {
			return nil
		}
		name, ok := unquotedString(text)
		if !ok || name == "" {
			return nil
		}
		return []string{name}
	}
	bindings := make([]string, 0, 4)
	childCount := analysis.boundedNamedChildCount(clause, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
	for index := uint(0); index < childCount; index++ {
		child := clause.NamedChild(index)
		if child == nil {
			continue
		}
		switch child.Kind() {
		case "identifier":
			if name, ok := analysis.stableName(child, "identifier"); ok {
				bindings = append(bindings, name)
			}
		case "namespace_import":
			if name, ok := analysis.stableName(child.NamedChild(0), "identifier"); ok {
				bindings = append(bindings, name)
			}
		case "named_imports":
			itemCount := analysis.boundedNamedChildCount(child, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
			for itemIndex := uint(0); itemIndex < itemCount; itemIndex++ {
				item := child.NamedChild(itemIndex)
				if item == nil || item.Kind() != "import_specifier" {
					continue
				}
				binding := item.ChildByFieldName("alias")
				if binding == nil {
					binding = item.ChildByFieldName("name")
				}
				if name, ok := analysis.stableName(binding, "identifier", "string"); ok {
					bindings = append(bindings, strings.Trim(name, "\"'"))
				}
			}
		}
	}
	return bindings
}

func ecmaDynamicLookup(analysis *treeSitterAnalysis, node *sitter.Node) bool {
	function := node.ChildByFieldName("function")
	if function == nil {
		return false
	}
	switch function.Kind() {
	case "import":
		return true
	case "identifier":
		name, ok := analysis.nodeText(function)
		if !ok {
			return false
		}
		if name == "eval" || name == "Function" {
			return true
		}
		if name == "require" {
			arguments := node.ChildByFieldName("arguments")
			if arguments == nil || arguments.NamedChildCount() != 1 {
				return true
			}
			return arguments.NamedChild(0).Kind() != "string"
		}
	case "member_expression":
		object, objectOK := analysis.nodeText(function.ChildByFieldName("object"))
		property, propertyOK := analysis.nodeText(function.ChildByFieldName("property"))
		return objectOK && propertyOK && object == "Reflect" && property != ""
	}
	return false
}
