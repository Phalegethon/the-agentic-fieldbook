package query

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// syntheticRecords mirrors the shape of the 100k synthetic repository used
// for the Phase 2 and 4a measurements (2,000 modules x 25 functions + 25
// classes) without running an extractor. It is deterministic so the oracle
// fixture and the benchmark see identical records.
func syntheticRecords(modules, functionsPerModule, classesPerModule int) []model.Record {
	records := make([]model.Record, 0, modules*(functionsPerModule+classesPerModule+1))
	identity := func(path, qualified string) string {
		sum := sha256.Sum256([]byte(path + "\x00" + qualified))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	for m := 0; m < modules; m++ {
		path := fmt.Sprintf("pkg/module_%04d.py", m)
		module := fmt.Sprintf("module_%04d", m)
		add := func(qualified string, kind model.RecordKind, line int, terms ...string) {
			id := identity(path, qualified)
			records = append(records, model.Record{
				Identity: id, Path: path, StartLine: line, EndLine: line + 2, Language: "python",
				RecordKind: kind, SourceType: "source", QualifiedName: qualified,
				ExtractionMethod: "tree-sitter-python@0.25.0", EvidenceClass: model.Verified,
				SearchTerms: terms, SourceDigest: id, Preview: qualified,
			})
		}
		add(module, model.Module, 1, module)
		for f := 0; f < functionsPerModule; f++ {
			name := fmt.Sprintf("function_%04d_%03d", m, f)
			add(module+"."+name, model.Definition, 10*f+2, "function", name, module)
		}
		for c := 0; c < classesPerModule; c++ {
			name := fmt.Sprintf("Widget%04d_%03d", m, c)
			add(module+"."+name, model.Definition, 10*(functionsPerModule+c)+2, "widget", name, module)
		}
	}
	return records
}

func syntheticSnapshot(records []model.Record) store.Snapshot {
	return store.Snapshot{Records: records, Query: store.BuildQueryIndex(records)}
}

type oracleQuery struct {
	Name      string         `json:"name"`
	Operation wire.Operation `json:"operation"`
	Query     string         `json:"query"`
	Filters   wire.Filters   `json:"filters"`
	Maximum   int            `json:"maximum_results"`
	Inferred  bool           `json:"allow_inferred"`
}

// oracleQueries returns the twenty requests shared by the search benchmark
// and Task 5's oracle fixture. The set and its order are stable: Task 5
// generates its fixture from this exact list.
func oracleQueries() []oracleQuery {
	none := wire.Filters{PathPrefixes: []string{}, Languages: []string{}, SymbolKinds: []string{}, SourceTypes: []string{}}
	with := func(mutate func(*wire.Filters)) wire.Filters { f := none; mutate(&f); return f }
	return []oracleQuery{
		{"exact-qualified", wire.SearchSymbols, "module_0123.function_0123_017", none, 8, false},
		{"exact-short-function", wire.SearchSymbols, "function_0123_017", none, 8, false},
		{"exact-short-class", wire.SearchSymbols, "Widget0123_017", none, 8, false},
		{"exact-module", wire.SearchSymbols, "module_0123", none, 8, false},
		{"prefix-narrow", wire.SearchSymbols, "function_0123_01", none, 8, false},
		{"prefix-wide", wire.SearchSymbols, "function_01", none, 8, false},
		{"prefix-widget-wide", wire.SearchSymbols, "Widget01", none, 64, false},
		{"broad-word-function", wire.SearchSymbols, "function", none, 8, false},
		{"broad-word-widget", wire.SearchSymbols, "widget", none, 8, false},
		{"substring", wire.SearchSymbols, "ion_0123_0", none, 8, false},
		{"fuzzy-typo", wire.SearchSymbols, "functoin_0123_017", none, 8, false},
		{"fuzzy-widget", wire.SearchSymbols, "Widgte0123_017", none, 8, false},
		{"multi-word-hit", wire.SearchSymbols, "module_0123 widget", none, 8, false},
		{"multi-word-miss", wire.SearchSymbols, "module_0123 nothing_here", none, 8, false},
		{"filter-language", wire.SearchSymbols, "function_0123_017", with(func(f *wire.Filters) { f.Languages = []string{"python"} }), 8, false},
		{"filter-kind", wire.SearchSymbols, "module_0123", with(func(f *wire.Filters) { f.SymbolKinds = []string{"definition"} }), 8, false},
		{"filter-path", wire.SearchSymbols, "function", with(func(f *wire.Filters) { f.PathPrefixes = []string{"pkg/module_00"} }), 8, false},
		{"filter-source", wire.SearchSymbols, "widget", with(func(f *wire.Filters) { f.SourceTypes = []string{"source"} }), 8, false},
		{"docs-miss", wire.SearchDocs, "function", none, 8, false},
		{"map-prefix", wire.RepositoryMap, "", with(func(f *wire.Filters) { f.PathPrefixes = []string{"pkg/module_001"} }), 16, false},
	}
}

func (item oracleQuery) request() wire.Request {
	query := item.Query
	var pointer *string
	if item.Operation != wire.RepositoryMap {
		pointer = &query
	}
	return wire.Request{
		SchemaVersion: "1", RequestIdentity: "oracle-" + item.Name, ConsumerIdentity: "oracle", Operation: item.Operation,
		RepositoryIdentity: "sha256:" + hexRepeat("a"), WorktreeIdentity: "sha256:" + hexRepeat("b"),
		CommittedHead: "0123456789abcdef0123456789abcdef01234567", DirtyOverlayFingerprint: "sha256:" + hexRepeat("c"),
		ProviderIdentity: "taf-context", RequiredCapability: string(item.Operation), MinimumFreshness: "exact",
		Query: pointer, ResultIdentities: []string{}, Filters: item.Filters, MaximumResults: item.Maximum,
		MaximumModelOutputCharacters: 4000, AllowInferred: item.Inferred,
	}
}

func hexRepeat(char string) string {
	out := make([]byte, 0, 64)
	for range 64 {
		out = append(out, char[0])
	}
	return string(out)
}
