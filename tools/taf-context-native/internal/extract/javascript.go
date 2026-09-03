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
  (new_expression)
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
		arrow := value != nil && value.Kind() == "arrow_function"
		if !arrow && !ecmaModuleScopeDeclarator(node) {
			return
		}
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier")
		if !ok {
			// Destructuring patterns have no single stable name. Only an arrow
			// function without one is worth a warning; module-scope patterns are
			// simply not indexed.
			if arrow {
				analysis.addWarning(warningPrefix + "-generated-name")
			}
			return
		}
		prefix, ok := ecmaLexicalPrefix(analysis, node)
		if !ok {
			return
		}
		analysis.appendNodeRecord(node, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "import_statement":
		for _, binding := range ecmaImportBindings(analysis, node) {
			analysis.appendImportRecord(node, binding.name, binding.target)
		}
	case "call_expression", "new_expression":
		if node.Kind() == "call_expression" && ecmaDynamicLookup(analysis, node) {
			analysis.addWarning(warningPrefix + "-dynamic-lookup")
		}
		enclosing, ok := analysis.enclosingScope(node, ecmaScope(analysis), nil)
		if !ok {
			return
		}
		callee := node.ChildByFieldName("function")
		if callee == nil {
			callee = node.ChildByFieldName("constructor")
		}
		target, _ := analysis.dottedTarget(callee, ecmaTargetRules, 0)
		analysis.appendReference(node, enclosing, target)
	}
}

var ecmaTargetRules = dottedTargetRules{
	leaf:       []string{"identifier", "property_identifier", "private_property_identifier", "this", "super"},
	containers: []dottedContainer{{kind: "member_expression", object: "object", property: "property"}},
}

func ecmaLexicalPrefix(analysis *treeSitterAnalysis, node *sitter.Node) ([]string, bool) {
	return analysis.lexicalPrefix(node, ecmaScope(analysis))
}

// ecmaScope names the definitions that lexically contain a node: classes,
// functions, methods, and arrow functions bound to a name.
func ecmaScope(analysis *treeSitterAnalysis) func(*sitter.Node) (string, bool) {
	return func(parent *sitter.Node) (string, bool) {
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
	}
}

// ecmaModuleScopeDeclarator reports whether a variable_declarator belongs to a
// declaration at the top of the module, directly or through an export
// statement. Module-scope constants are how stores, providers, routers, and
// styled components are defined in JavaScript and TypeScript, so they are
// definitions; function-local variables are not.
func ecmaModuleScopeDeclarator(node *sitter.Node) bool {
	declaration := node.Parent()
	if declaration == nil || (declaration.Kind() != "lexical_declaration" && declaration.Kind() != "variable_declaration") {
		return false
	}
	container := declaration.Parent()
	if container == nil {
		return false
	}
	if container.Kind() == "export_statement" {
		container = container.Parent()
	}
	return container != nil && container.Kind() == "program"
}

func ecmaImportBindings(analysis *treeSitterAnalysis, node *sitter.Node) []importBinding {
	specifier := ""
	if text, ok := analysis.nodeText(node.ChildByFieldName("source")); ok {
		if unquoted, unquotedOK := unquotedString(text); unquotedOK {
			specifier = unquoted
		}
	}
	clause := analysis.childByKind(node, "import_clause")
	if clause == nil {
		if specifier == "" {
			return nil
		}
		return []importBinding{{name: specifier, target: specifier}}
	}
	bindings := make([]importBinding, 0, 4)
	childCount := analysis.boundedNamedChildCount(clause, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
	for index := uint(0); index < childCount; index++ {
		child := clause.NamedChild(index)
		if child == nil {
			continue
		}
		switch child.Kind() {
		case "identifier":
			if name, ok := analysis.stableName(child, "identifier"); ok {
				bindings = append(bindings, importBinding{name: name, target: specifier})
			}
		case "namespace_import":
			if name, ok := analysis.stableName(child.NamedChild(0), "identifier"); ok {
				bindings = append(bindings, importBinding{name: name, target: specifier})
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
					bindings = append(bindings, importBinding{name: strings.Trim(name, "\"'"), target: specifier})
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
