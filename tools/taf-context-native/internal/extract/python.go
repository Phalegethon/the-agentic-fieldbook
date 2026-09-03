package extract

import (
	"context"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	sitter "github.com/tree-sitter/go-tree-sitter"
	python "github.com/tree-sitter/tree-sitter-python/bindings/go"
)

const pythonParserVersion = "tree-sitter-python@0.25.0"

const pythonQuery = `
[
  (class_definition)
  (function_definition)
  (import_statement)
  (import_from_statement)
  (call)
  (subscript)
] @item
`

type pythonExtractor struct{ extensions []string }

func (extractor pythonExtractor) Language() string      { return "python" }
func (extractor pythonExtractor) ParserVersion() string { return pythonParserVersion }
func (extractor pythonExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor pythonExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}
func (extractor pythonExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	return extractor.ExtractContext(context.Background(), file)
}
func (extractor pythonExtractor) ExtractContext(ctx context.Context, file boundary.StableFile) ([]model.Record, Report) {
	return extractTreeSitter(ctx, file, treeSitterGrammar{
		language:      sitter.NewLanguage(python.Language()),
		query:         pythonQuery,
		parserVersion: pythonParserVersion,
		warningPrefix: "python",
		handle:        handlePythonNode,
	})
}

func handlePythonNode(analysis *treeSitterAnalysis, node *sitter.Node) {
	switch node.Kind() {
	case "class_definition", "function_definition":
		name, ok := analysis.stableName(node.ChildByFieldName("name"), "identifier")
		if !ok {
			analysis.addWarning("python-generated-name")
			return
		}
		prefix, ok := analysis.lexicalPrefix(node, pythonScope(analysis))
		if !ok {
			return
		}
		analysis.appendNodeRecord(pythonDefinitionRange(node), analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "import_statement", "import_from_statement":
		for _, binding := range pythonImportBindings(analysis, node) {
			analysis.appendImportRecord(node, binding.name, binding.target)
		}
	case "call", "subscript":
		if pythonDynamicLookup(analysis, node) {
			analysis.addWarning("python-dynamic-lookup")
		}
		if node.Kind() != "call" {
			return
		}
		enclosing, ok := analysis.enclosingScope(node, pythonScope(analysis), pythonDefinitionRange)
		if !ok {
			return
		}
		target, _ := analysis.dottedTarget(node.ChildByFieldName("function"), pythonTargetRules, 0)
		analysis.appendReference(node, enclosing, target)
	}
}

// pythonDefinitionRange is the node whose range a class or function record
// carries: the decorated definition when the definition is decorated, so the
// decorators belong to the record.
func pythonDefinitionRange(node *sitter.Node) *sitter.Node {
	parent := node.Parent()
	if parent == nil || parent.Kind() != "decorated_definition" || parent.HasError() || parent.IsMissing() {
		return node
	}
	if definition := parent.ChildByFieldName("definition"); definition != nil && definition.Id() == node.Id() {
		return parent
	}
	return node
}

// pythonScope names the definitions that lexically contain a node: classes and
// functions, in source order from the outermost one.
func pythonScope(analysis *treeSitterAnalysis) func(*sitter.Node) (string, bool) {
	return func(parent *sitter.Node) (string, bool) {
		if parent.Kind() != "class_definition" && parent.Kind() != "function_definition" {
			return "", false
		}
		return analysis.stableName(parent.ChildByFieldName("name"), "identifier")
	}
}

var pythonTargetRules = dottedTargetRules{
	leaf:       []string{"identifier"},
	containers: []dottedContainer{{kind: "attribute", object: "object", property: "attribute"}},
}

// importBinding is one local name an import statement binds together with the
// module specifier it was imported from, as written in the source.
type importBinding struct {
	name   string
	target string
}

func pythonImportBindings(analysis *treeSitterAnalysis, node *sitter.Node) []importBinding {
	bindings := make([]importBinding, 0, 4)
	module := node.ChildByFieldName("module_name")
	fromImport := node.Kind() == "import_from_statement"
	moduleName := ""
	if fromImport {
		if text, ok := analysis.nodeText(module); ok && text != "" && !strings.ContainsAny(text, "\x00\n\r") {
			moduleName = text
		}
	}
	childCount := analysis.boundedNamedChildCount(node, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
	for index := uint(0); index < childCount; index++ {
		child := node.NamedChild(index)
		if child == nil || (module != nil && child.Id() == module.Id()) {
			continue
		}
		switch child.Kind() {
		case "aliased_import":
			alias, ok := analysis.stableName(child.ChildByFieldName("alias"), "identifier")
			if !ok {
				continue
			}
			target := moduleName
			if !fromImport {
				if text, textOK := analysis.nodeText(child.ChildByFieldName("name")); textOK && text != "" && !strings.ContainsAny(text, "\x00\n\r") {
					target = text
				} else {
					target = ""
				}
			}
			bindings = append(bindings, importBinding{name: alias, target: target})
		case "dotted_name":
			if name, ok := analysis.nodeText(child); ok && name != "" && !strings.ContainsAny(name, "\x00\n\r") {
				target := moduleName
				if !fromImport {
					target = name
				} else if last := strings.LastIndexByte(name, '.'); last >= 0 {
					name = name[last+1:]
				}
				bindings = append(bindings, importBinding{name: name, target: target})
			}
		case "wildcard_import":
			if moduleName != "" {
				bindings = append(bindings, importBinding{name: moduleName + ".*", target: moduleName})
			}
		}
	}
	return bindings
}

func pythonDynamicLookup(analysis *treeSitterAnalysis, node *sitter.Node) bool {
	switch node.Kind() {
	case "call":
		function := node.ChildByFieldName("function")
		name, ok := analysis.nodeText(function)
		if !ok {
			return false
		}
		switch name {
		case "__import__", "eval", "exec", "getattr", "setattr", "globals", "locals", "importlib.import_module":
			return true
		}
	case "subscript":
		value := node.ChildByFieldName("value")
		if value == nil || value.Kind() != "call" {
			return false
		}
		function, ok := analysis.nodeText(value.ChildByFieldName("function"))
		return ok && (function == "globals" || function == "locals")
	}
	return false
}
