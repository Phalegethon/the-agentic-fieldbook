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
	// A use outside any declaration belongs to the module record, so it
	// carries that record's name and range.
	moduleScope := referenceScope{name: packageName, start: start, end: end}
	for _, imported := range parsed.Imports {
		name := goImportName(imported)
		if name == "" {
			continue
		}
		start, end = astLines(fileSet, imported.Pos(), imported.End())
		record := structuralRecord(name, model.Import, "source", start, end)
		if specifier, unquoteErr := strconv.Unquote(imported.Path.Value); unquoteErr == nil {
			record.TargetName = boundedImportSpecifier(specifier)
		}
		if !appendRecord(record) {
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
			} else if packageName == "main" && node.Name.Name == "main" && goMainEntryPointSignature(node.Type) {
				kind = model.EntryPoint
			}
			start, end = astLines(fileSet, node.Pos(), node.End())
			if !appendRecord(structuralRecord(qualified, kind, "source", start, end)) {
				break declarations
			}
		}
	}
	warnings = append(warnings, goCollectReferences(fileSet, parsed, moduleScope, appendRecord)...)
	return records, Report{ParserVersion: goParserVersion, WarningCodes: warnings}
}

// goCollectReferences records where each declaration uses another name. Calls
// inside a function body belong to that function; calls in a package-level
// variable initializer belong to the file's package record. Every target of
// one declaration is merged into that declaration's single reference record.
func goCollectReferences(fileSet *token.FileSet, parsed *ast.File, moduleScope referenceScope, appendRecord func(model.Record) bool) []string {
	collector := referenceCollector{}
	collect := func(scope referenceScope, node ast.Node) {
		if node == nil || scope.name == "" {
			return
		}
		ast.Inspect(node, func(visited ast.Node) bool {
			call, ok := visited.(*ast.CallExpr)
			if !ok {
				return true
			}
			target, named := goCallTarget(call.Fun, 0)
			if !named {
				target = ""
			}
			line, _ := astLines(fileSet, call.Pos(), call.End())
			collector.add(scope, target, line)
			return true
		})
	}
	for _, declaration := range parsed.Decls {
		switch node := declaration.(type) {
		case *ast.GenDecl:
			if node.Tok != token.VAR && node.Tok != token.CONST {
				continue
			}
			for _, item := range node.Specs {
				value, ok := item.(*ast.ValueSpec)
				if !ok {
					continue
				}
				for _, expression := range value.Values {
					collect(moduleScope, expression)
				}
			}
		case *ast.FuncDecl:
			start, end := astLines(fileSet, node.Pos(), node.End())
			collect(referenceScope{name: goDeclarationName(moduleScope.name, node), start: start, end: end}, node.Body)
		}
	}
	records, warnings := collector.flush()
	for _, record := range records {
		if !appendRecord(record) {
			break
		}
	}
	return warnings
}

// goDeclarationName is the qualified name the declaration's own definition
// record carries, so a reference names its enclosing definition exactly.
func goDeclarationName(packageName string, node *ast.FuncDecl) string {
	if node.Recv != nil && len(node.Recv.List) != 0 {
		receiver := goReceiverName(node.Recv.List[0].Type)
		if receiver == "" {
			return ""
		}
		return packageName + "." + receiver + "." + node.Name.Name
	}
	return packageName + "." + node.Name.Name
}

// goCallTarget renders a call target as the dotted name it is written with.
// A computed callee - an index expression, a call returning a function, a
// literal - has no stable written name.
func goCallTarget(expression ast.Expr, depth int) (string, bool) {
	if depth > maximumDottedTargetDepth {
		return "", false
	}
	switch node := expression.(type) {
	case *ast.Ident:
		return node.Name, true
	case *ast.SelectorExpr:
		qualifier, ok := goCallTarget(node.X, depth+1)
		if !ok || node.Sel == nil {
			return "", false
		}
		return qualifier + "." + node.Sel.Name, true
	default:
		return "", false
	}
}

func goMainEntryPointSignature(function *ast.FuncType) bool {
	if function == nil {
		return false
	}
	return goFieldListEmpty(function.TypeParams) && goFieldListEmpty(function.Params) && goFieldListEmpty(function.Results)
}

func goFieldListEmpty(fields *ast.FieldList) bool {
	return fields == nil || len(fields.List) == 0
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
