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
		prefix, ok := analysis.lexicalPrefix(node, func(parent *sitter.Node) (string, bool) {
			if parent.Kind() != "class_definition" && parent.Kind() != "function_definition" {
				return "", false
			}
			return analysis.stableName(parent.ChildByFieldName("name"), "identifier")
		})
		if !ok {
			return
		}
		rangeNode := node
		if parent := node.Parent(); parent != nil && parent.Kind() == "decorated_definition" {
			definition := parent.ChildByFieldName("definition")
			if definition != nil && definition.Id() == node.Id() && !parent.HasError() && !parent.IsMissing() {
				rangeNode = parent
			}
		}
		analysis.appendNodeRecord(rangeNode, analysis.qualified(append(prefix, name)...), model.Definition, model.Verified)
	case "import_statement", "import_from_statement":
		for _, binding := range pythonImportBindings(analysis, node) {
			analysis.appendNodeRecord(node, binding, model.Import, model.Verified)
		}
	case "call", "subscript":
		if pythonDynamicLookup(analysis, node) {
			analysis.addWarning("python-dynamic-lookup")
		}
	}
}

func pythonImportBindings(analysis *treeSitterAnalysis, node *sitter.Node) []string {
	bindings := make([]string, 0, 4)
	module := node.ChildByFieldName("module_name")
	childCount := analysis.boundedNamedChildCount(node, maximumTreeSitterImportNodes, "tree-sitter-import-limit")
	for index := uint(0); index < childCount; index++ {
		child := node.NamedChild(index)
		if child == nil || (module != nil && child.Id() == module.Id()) {
			continue
		}
		switch child.Kind() {
		case "aliased_import":
			if alias, ok := analysis.stableName(child.ChildByFieldName("alias"), "identifier"); ok {
				bindings = append(bindings, alias)
			}
		case "dotted_name":
			if name, ok := analysis.nodeText(child); ok && name != "" && !strings.ContainsAny(name, "\x00\n\r") {
				if node.Kind() == "import_from_statement" {
					if last := strings.LastIndexByte(name, '.'); last >= 0 {
						name = name[last+1:]
					}
				}
				bindings = append(bindings, name)
			}
		case "wildcard_import":
			if moduleName, ok := analysis.nodeText(module); ok {
				bindings = append(bindings, moduleName+".*")
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
