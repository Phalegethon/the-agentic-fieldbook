package extract

import (
	"context"
	"path"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	sitter "github.com/tree-sitter/go-tree-sitter"
	typescript "github.com/tree-sitter/tree-sitter-typescript/bindings/go"
)

const typescriptParserVersion = "tree-sitter-typescript@0.23.2"

const typescriptQuery = `
[
  (class_declaration)
  (function_declaration)
  (method_definition)
  (variable_declarator)
  (interface_declaration)
  (type_alias_declaration)
  (import_statement)
  (call_expression)
  (new_expression)
] @item
`

type typescriptExtractor struct{ extensions []string }

func (extractor typescriptExtractor) Language() string      { return "typescript" }
func (extractor typescriptExtractor) ParserVersion() string { return typescriptParserVersion }
func (extractor typescriptExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor typescriptExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}
func (extractor typescriptExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	return extractor.ExtractContext(context.Background(), file)
}
func (extractor typescriptExtractor) ExtractContext(ctx context.Context, file boundary.StableFile) ([]model.Record, Report) {
	language := sitter.NewLanguage(typescript.LanguageTypescript())
	if path.Ext(file.RelativePath) == ".tsx" {
		language = sitter.NewLanguage(typescript.LanguageTSX())
	}
	return extractTreeSitter(ctx, file, treeSitterGrammar{
		language:      language,
		query:         typescriptQuery,
		parserVersion: typescriptParserVersion,
		warningPrefix: "typescript",
		handle: func(analysis *treeSitterAnalysis, node *sitter.Node) {
			handleECMAScriptNode(analysis, node, true, "typescript")
		},
	})
}
