package extract

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const goParserVersion = "go/parser@go1.27"
const maximumGoRecords = 4096

type goExtractor struct {
	extensions []string
}

func (extractor goExtractor) Language() string      { return "go" }
func (extractor goExtractor) ParserVersion() string { return goParserVersion }
func (extractor goExtractor) Extensions() []string {
	return append([]string(nil), extractor.extensions...)
}
func (extractor goExtractor) MaximumBytes() int64 {
	return int64(policy.ProductionLimits().MaximumSourceFileBytes)
}

func (extractor goExtractor) Extract(file boundary.StableFile) ([]model.Record, Report) {
	fileSet := token.NewFileSet()
	parsed, err := parser.ParseFile(fileSet, file.RelativePath, file.Bytes, parser.AllErrors)
	if err != nil || parsed == nil {
		return nil, Report{ParserVersion: goParserVersion, ParseFailures: 1, WarningCodes: []string{"go-parse-failure"}}
	}
	records := make([]model.Record, 0, len(parsed.Decls)+len(parsed.Imports)+1)
	warnings := []string{}
	limited := false
	appendRecord := func(record model.Record) bool {
		if len(records) >= maximumGoRecords {
			if !limited {
				warnings = append(warnings, "go-record-limit")
				limited = true
			}
			return false
		}
		records = append(records, record)
		return true
	}
	packageName := parsed.Name.Name
	start, end := astLines(fileSet, parsed.Package, parsed.Name.End())
	appendRecord(structuralRecord(packageName, model.Module, "source", start, end))
	for _, imported := range parsed.Imports {
		name := goImportName(imported)
		if name == "" {
			continue
		}
		start, end = astLines(fileSet, imported.Pos(), imported.End())
		if !appendRecord(structuralRecord(name, model.Import, "source", start, end)) {
			break
		}
	}
declarations:
	for _, declaration := range parsed.Decls {
		switch node := declaration.(type) {
		case *ast.GenDecl:
			if node.Tok != token.TYPE {
				continue
			}
			for _, item := range node.Specs {
				typeSpec, ok := item.(*ast.TypeSpec)
				if !ok {
					continue
				}
				start, end = astLines(fileSet, typeSpec.Pos(), typeSpec.End())
				if !appendRecord(structuralRecord(packageName+"."+typeSpec.Name.Name, model.Definition, "source", start, end)) {
					break declarations
				}
			}
		case *ast.FuncDecl:
			qualified := packageName + "." + node.Name.Name
			kind := model.Definition
			if node.Recv != nil && len(node.Recv.List) != 0 {
				receiver := goReceiverName(node.Recv.List[0].Type)
				if receiver == "" {
					warnings = append(warnings, "go-unsupported-receiver")
					continue
				}
				qualified = packageName + "." + receiver + "." + node.Name.Name
			} else if packageName == "main" && node.Name.Name == "main" {
				kind = model.EntryPoint
			}
			start, end = astLines(fileSet, node.Pos(), node.End())
			if !appendRecord(structuralRecord(qualified, kind, "source", start, end)) {
				break declarations
			}
		}
	}
	return records, Report{ParserVersion: goParserVersion, WarningCodes: warnings}
}

func structuralRecord(qualified string, kind model.RecordKind, sourceType string, start, end int) model.Record {
	return model.Record{
		StartLine:     start,
		EndLine:       end,
		RecordKind:    kind,
		SourceType:    sourceType,
		QualifiedName: qualified,
		EvidenceClass: model.Verified,
	}
}

func astLines(fileSet *token.FileSet, start, end token.Pos) (int, int) {
	return fileSet.PositionFor(start, false).Line, fileSet.PositionFor(end, false).Line
}

func goImportName(imported *ast.ImportSpec) string {
	if imported.Name != nil {
		if imported.Name.Name == "_" || imported.Name.Name == "." {
			value, err := strconv.Unquote(imported.Path.Value)
			if err != nil {
				return ""
			}
			return value
		}
		return imported.Name.Name
	}
	value, err := strconv.Unquote(imported.Path.Value)
	if err != nil {
		return ""
	}
	return value
}

func goReceiverName(expression ast.Expr) string {
	switch node := expression.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.StarExpr:
		return goReceiverName(node.X)
	case *ast.ParenExpr:
		return goReceiverName(node.X)
	case *ast.IndexExpr:
		return goReceiverName(node.X)
	case *ast.IndexListExpr:
		return goReceiverName(node.X)
	default:
		return ""
	}
}
