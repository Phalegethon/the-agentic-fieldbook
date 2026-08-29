package extract

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	sitter "github.com/tree-sitter/go-tree-sitter"
	python "github.com/tree-sitter/tree-sitter-python/bindings/go"
)

func TestPythonExtractorUsesDecoratedAsyncAndLexicalRanges(t *testing.T) {
	source := "from functools import wraps as decorator\n" +
		"import os.path as osp\n" +
		"\n" +
		"@decorator\n" +
		"class Registry:\n" +
		"    async def load(self):\n" +
		"        def nested():\n" +
		"            return 1\n" +
		"        return nested()\n" +
		"\n" +
		"async def top():\n" +
		"    return None\n"
	records, report := NewRegistry().Extract(stableFile("pkg/service.py", source))
	if report.ParserVersion != pythonParserVersion || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, want := range []recordExpectation{
		{"decorator", model.Import, "source", 1, 1, "python", pythonParserVersion, model.Verified},
		{"osp", model.Import, "source", 2, 2, "python", pythonParserVersion, model.Verified},
		{"service.Registry", model.Definition, "source", 4, 9, "python", pythonParserVersion, model.Verified},
		{"service.Registry.load", model.Definition, "source", 6, 9, "python", pythonParserVersion, model.Verified},
		{"service.Registry.load.nested", model.Definition, "source", 7, 8, "python", pythonParserVersion, model.Verified},
		{"service.top", model.Definition, "source", 11, 12, "python", pythonParserVersion, model.Verified},
	} {
		assertRecord(t, records, want)
	}
}

func TestPythonDynamicLookupCannotBecomeVerified(t *testing.T) {
	source := "class Registry:\n    pass\nvalue = globals()[name]\nmodule = __import__(module_name)\n"
	records, report := NewRegistry().Extract(stableFile("pkg/registry.py", source))
	record := findRecord(t, records, "registry.Registry")
	if record.EvidenceClass == model.Verified {
		t.Fatalf("dynamic lookup left a verified record: %#v", record)
	}
	if !contains(report.WarningCodes, "python-dynamic-lookup") {
		t.Fatalf("report = %#v", report)
	}
}

func TestJavaScriptExtractorUsesImportsExportsMethodsAndJSX(t *testing.T) {
	source := "import defaultThing, { named as alias } from \"pkg\";\n" +
		"import * as ns from \"scope\";\n" +
		"export class Service {\n" +
		"  run() {\n" +
		"    function nested() {}\n" +
		"  }\n" +
		"}\n" +
		"export function top() {}\n" +
		"export const arrow = () => {\n" +
		"  return <View />;\n" +
		"};\n"
	records, report := NewRegistry().Extract(stableFile("web/component.jsx", source))
	if report.ParserVersion != javascriptParserVersion || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, want := range []recordExpectation{
		{"defaultThing", model.Import, "source", 1, 1, "javascript", javascriptParserVersion, model.Verified},
		{"alias", model.Import, "source", 1, 1, "javascript", javascriptParserVersion, model.Verified},
		{"ns", model.Import, "source", 2, 2, "javascript", javascriptParserVersion, model.Verified},
		{"component.Service", model.Definition, "source", 3, 7, "javascript", javascriptParserVersion, model.Verified},
		{"component.Service.run", model.Definition, "source", 4, 6, "javascript", javascriptParserVersion, model.Verified},
		{"component.Service.run.nested", model.Definition, "source", 5, 5, "javascript", javascriptParserVersion, model.Verified},
		{"component.top", model.Definition, "source", 8, 8, "javascript", javascriptParserVersion, model.Verified},
		{"component.arrow", model.Definition, "source", 9, 11, "javascript", javascriptParserVersion, model.Verified},
	} {
		assertRecord(t, records, want)
	}
}

func TestJavaScriptReflectionAndDynamicImportCannotBecomeVerified(t *testing.T) {
	source := "export class Registry {}\nconst module = import(name);\nconst value = Reflect.get(object, key);\n"
	records, report := NewRegistry().Extract(stableFile("web/registry.js", source))
	if record := findRecord(t, records, "registry.Registry"); record.EvidenceClass == model.Verified {
		t.Fatalf("reflection left a verified record: %#v", record)
	}
	if !contains(report.WarningCodes, "javascript-dynamic-lookup") {
		t.Fatalf("report = %#v", report)
	}
}

func TestTypeScriptExtractorUsesTSXAndTypeDefinitions(t *testing.T) {
	source := "import { Thing as Alias } from \"pkg\";\n" +
		"export interface API {\n" +
		"  run(): void;\n" +
		"}\n" +
		"export type ID = string;\n" +
		"export class Service {\n" +
		"  render(): JSX.Element {\n" +
		"    return <View />;\n" +
		"  }\n" +
		"}\n" +
		"export const factory = (): API => ({ run() {} });\n"
	records, report := NewRegistry().Extract(stableFile("web/component.tsx", source))
	if report.ParserVersion != typescriptParserVersion || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, want := range []recordExpectation{
		{"Alias", model.Import, "source", 1, 1, "typescript", typescriptParserVersion, model.Verified},
		{"component.API", model.Definition, "source", 2, 4, "typescript", typescriptParserVersion, model.Verified},
		{"component.ID", model.Definition, "source", 5, 5, "typescript", typescriptParserVersion, model.Verified},
		{"component.Service", model.Definition, "source", 6, 10, "typescript", typescriptParserVersion, model.Verified},
		{"component.Service.render", model.Definition, "source", 7, 9, "typescript", typescriptParserVersion, model.Verified},
		{"component.factory", model.Definition, "source", 11, 11, "typescript", typescriptParserVersion, model.Verified},
	} {
		assertRecord(t, records, want)
	}
}

func TestRustExtractorUsesItemsImplContainmentAliasesAndMacros(t *testing.T) {
	source := "use crate::config::Settings as Config;\n" +
		"use crate::models::{User, Role as UserRole};\n" +
		"struct Service { value: usize }\n" +
		"enum Mode { Fast, Safe }\n" +
		"trait Runner {\n" +
		"    fn run(&self);\n" +
		"}\n" +
		"impl Service {\n" +
		"    fn run(&self) { generated!(); }\n" +
		"}\n" +
		"fn helper() {}\n" +
		"macro_rules! generated { () => {} }\n"
	records, report := NewRegistry().Extract(stableFile("src/service.rs", source))
	if report.ParserVersion != rustParserVersion || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, want := range []recordExpectation{
		{"Config", model.Import, "source", 1, 1, "rust", rustParserVersion, model.Verified},
		{"crate::models::User", model.Import, "source", 2, 2, "rust", rustParserVersion, model.Verified},
		{"UserRole", model.Import, "source", 2, 2, "rust", rustParserVersion, model.Verified},
		{"service.Service", model.Definition, "source", 3, 3, "rust", rustParserVersion, model.Verified},
		{"service.Mode", model.Definition, "source", 4, 4, "rust", rustParserVersion, model.Verified},
		{"service.Runner", model.Definition, "source", 5, 7, "rust", rustParserVersion, model.Verified},
		{"service.Runner.run", model.Definition, "source", 6, 6, "rust", rustParserVersion, model.Verified},
		{"service.Service.run", model.Definition, "source", 9, 9, "rust", rustParserVersion, model.Verified},
		{"service.helper", model.Definition, "source", 11, 11, "rust", rustParserVersion, model.Verified},
		{"service.generated", model.Definition, "source", 12, 12, "rust", rustParserVersion, model.Verified},
	} {
		assertRecord(t, records, want)
	}
	invocation := findRecord(t, records, "service.Service.run.generated!")
	if invocation.EvidenceClass != model.Inferred {
		t.Fatalf("macro invocation = %#v, want inferred", invocation)
	}
}

func TestTreeSitterSyntaxErrorsNeverOverlapVerifiedRecords(t *testing.T) {
	fixtures := []struct {
		path      string
		source    string
		qualified string
	}{
		{"pkg/good.py", "class Good:\n    pass\ndef broken(\n", "good.Good"},
		{"web/good.js", "class Good {}\nfunction broken( {\n", "good.Good"},
		{"web/good.ts", "interface Good {}\nconst broken: = 1;\n", "good.Good"},
		{"src/good.rs", "struct Good;\nfn broken( {\n", "good.Good"},
	}
	for _, fixture := range fixtures {
		t.Run(fixture.path, func(t *testing.T) {
			records, report := NewRegistry().Extract(stableFile(fixture.path, fixture.source))
			if report.ParseFailures != 1 {
				t.Fatalf("report = %#v", report)
			}
			if record := findRecord(t, records, fixture.qualified); record.EvidenceClass != model.Verified {
				t.Fatalf("valid sibling = %#v", record)
			}
			for _, record := range records {
				if record.StartLine >= 2 && record.EvidenceClass == model.Verified && record.QualifiedName != fixture.qualified {
					t.Fatalf("error-overlapping record became verified: %#v", record)
				}
			}
		})
	}
}

func TestTreeSitterCancellationAndDeterministicWorkLimits(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	records, report := NewRegistry().ExtractContext(ctx, stableFile("pkg/cancel.py", "class Cancelled:\n    pass\n"))
	if len(records) != 0 || report.ParseFailures != 1 || !contains(report.WarningCodes, "tree-sitter-cancelled") {
		t.Fatalf("cancelled extraction = records %#v report %#v", records, report)
	}

	var source strings.Builder
	for index := 0; index <= maximumTreeSitterRecords; index++ {
		fmt.Fprintf(&source, "def f%d(): pass\n", index)
	}
	records, report = NewRegistry().Extract(stableFile("pkg/wide.py", source.String()))
	if len(records) != maximumTreeSitterRecords || report.ParseFailures != 1 || !contains(report.WarningCodes, "tree-sitter-record-limit") {
		t.Fatalf("wide extraction = %d records, report %#v", len(records), report)
	}

	source.Reset()
	source.WriteString("import ")
	for index := 0; index <= maximumTreeSitterImportNodes; index++ {
		if index != 0 {
			source.WriteString(", ")
		}
		fmt.Fprintf(&source, "module%d", index)
	}
	source.WriteByte('\n')
	records, report = NewRegistry().Extract(stableFile("pkg/imports.py", source.String()))
	if len(records) != maximumTreeSitterImportNodes || report.ParseFailures != 1 || !contains(report.WarningCodes, "tree-sitter-import-limit") {
		t.Fatalf("wide import extraction = %d records, report %#v", len(records), report)
	}

	source.Reset()
	for depth := 0; depth <= maximumTreeSitterDepth; depth++ {
		fmt.Fprintf(&source, "%sdef f%d():\n", strings.Repeat("    ", depth), depth)
	}
	fmt.Fprintf(&source, "%spass\n", strings.Repeat("    ", maximumTreeSitterDepth+1))
	records, report = NewRegistry().Extract(stableFile("pkg/deep.py", source.String()))
	if len(records) > maximumTreeSitterRecords || !contains(report.WarningCodes, "tree-sitter-depth-limit") {
		t.Fatalf("deep extraction = %d records, report %#v", len(records), report)
	}

	source.Reset()
	source.WriteString("use ")
	for depth := 0; depth <= maximumTreeSitterDepth; depth++ {
		fmt.Fprintf(&source, "n%d::{", depth)
	}
	source.WriteString("Leaf")
	source.WriteString(strings.Repeat("}", maximumTreeSitterDepth+1))
	source.WriteString(";\n")
	records, report = NewRegistry().Extract(stableFile("src/deep.rs", source.String()))
	if len(records) != 0 || !contains(report.WarningCodes, "tree-sitter-depth-limit") {
		t.Fatalf("deep Rust import extraction = %d records, report %#v", len(records), report)
	}
}

func TestTreeSitterParseCancellationOccursInsideNativeProgress(t *testing.T) {
	ctx := newNthErrCancellationContext(3)
	source := []byte(strings.Repeat("def parsed():\n    pass\n", 10_000))
	tree, err := parseTree(ctx, sitter.NewLanguage(python.Language()), source)
	if tree != nil {
		tree.Close()
	}
	if tree != nil || !errors.Is(err, errTreeSitterCancelled) || ctx.checks.Load() < 3 {
		t.Fatalf("parse cancellation = tree %v err %v checks %d", tree, err, ctx.checks.Load())
	}
}

func TestTreeSitterCancellationAfterFinalQueryNextReturnsNoPartials(t *testing.T) {
	ctx := newArmedEndCancellationContext()
	file := stableFile("pkg/final.py", "class Final:\n    pass\n")
	records, report := extractTreeSitter(ctx, file, treeSitterGrammar{
		language:      sitter.NewLanguage(python.Language()),
		query:         "(class_definition) @item",
		parserVersion: pythonParserVersion,
		warningPrefix: "python",
		handle: func(analysis *treeSitterAnalysis, node *sitter.Node) {
			analysis.appendNodeRecord(node, analysis.qualified("Final"), model.Definition, model.Verified)
			ctx.arm()
		},
	})
	if len(records) != 0 || report.ParseFailures != 1 || !contains(report.WarningCodes, "tree-sitter-cancelled") {
		t.Fatalf("end cancellation = records %#v report %#v checks %d", records, report, ctx.checks.Load())
	}
}

func TestTreeSitterProgressPayloadsAreReleasedByBinding(t *testing.T) {
	t.Run("parse", func(t *testing.T) {
		marker, collected := newCallbackMarker()
		exerciseParseProgressPayload(t, marker)
		waitForCallbackMarker(t, collected)
	})
	t.Run("query replacements and close", func(t *testing.T) {
		markers := make([]*callbackMarker, 3)
		collected := make([]<-chan struct{}, len(markers))
		for index := range markers {
			markers[index], collected[index] = newCallbackMarker()
		}
		exerciseQueryProgressPayloads(t, markers)
		markers = nil
		for index, markerCollected := range collected {
			if !callbackMarkerCollected(markerCollected) {
				for attempts := 0; attempts < 50 && !callbackMarkerCollected(markerCollected); attempts++ {
					runtime.GC()
					runtime.Gosched()
				}
			}
			if !callbackMarkerCollected(markerCollected) {
				t.Fatalf("query callback payload %d remains retained after replacement/close", index)
			}
		}
	})
}

func TestTreeSitterQueryCursorCloseIsIdempotent(t *testing.T) {
	if os.Getenv("TAF_TEST_QUERY_CURSOR_DOUBLE_CLOSE") == "1" {
		exerciseQueryCursorDoubleClose()
		return
	}
	command := exec.Command(os.Args[0], "-test.run=^TestTreeSitterQueryCursorCloseIsIdempotent$")
	command.Env = append(os.Environ(), "TAF_TEST_QUERY_CURSOR_DOUBLE_CLOSE=1")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("double close failed: %v\n%s", err, output)
	}
}

func TestTreeSitterConcurrentCancellationHasNoWatcherLeak(t *testing.T) {
	source := []byte(strings.Repeat("def cancelled():\n    pass\n", 2_000))
	before := runtime.NumGoroutine()
	var wait sync.WaitGroup
	failures := make(chan error, 16)
	for worker := 0; worker < 16; worker++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			for iteration := 0; iteration < 10; iteration++ {
				ctx := newNthErrCancellationContext(3)
				tree, err := parseTree(ctx, sitter.NewLanguage(python.Language()), source)
				if tree != nil {
					tree.Close()
				}
				if tree != nil || !errors.Is(err, errTreeSitterCancelled) {
					failures <- fmt.Errorf("tree %v err %v checks %d", tree, err, ctx.checks.Load())
					return
				}
			}
		}()
	}
	wait.Wait()
	close(failures)
	for err := range failures {
		t.Error(err)
	}
	runtime.GC()
	deadline := time.Now().Add(2 * time.Second)
	for runtime.NumGoroutine() > before+2 && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if after := runtime.NumGoroutine(); after > before+2 {
		t.Fatalf("goroutines after concurrent cancellation = %d, before = %d", after, before)
	}
}

type nthErrCancellationContext struct {
	threshold int32
	checks    atomic.Int32
	closed    atomic.Bool
	done      chan struct{}
}

func newNthErrCancellationContext(threshold int32) *nthErrCancellationContext {
	return &nthErrCancellationContext{threshold: threshold, done: make(chan struct{})}
}

func (ctx *nthErrCancellationContext) Deadline() (time.Time, bool) { return time.Time{}, false }
func (ctx *nthErrCancellationContext) Done() <-chan struct{}       { return ctx.done }
func (ctx *nthErrCancellationContext) Value(any) any               { return nil }
func (ctx *nthErrCancellationContext) Err() error {
	if ctx.checks.Add(1) < ctx.threshold {
		return nil
	}
	if ctx.closed.CompareAndSwap(false, true) {
		close(ctx.done)
	}
	return context.Canceled
}

type armedEndCancellationContext struct {
	armed  atomic.Bool
	checks atomic.Int32
	closed atomic.Bool
	done   chan struct{}
}

func newArmedEndCancellationContext() *armedEndCancellationContext {
	return &armedEndCancellationContext{done: make(chan struct{})}
}

func (ctx *armedEndCancellationContext) Deadline() (time.Time, bool) { return time.Time{}, false }
func (ctx *armedEndCancellationContext) Done() <-chan struct{}       { return ctx.done }
func (ctx *armedEndCancellationContext) Value(any) any               { return nil }
func (ctx *armedEndCancellationContext) arm()                        { ctx.armed.Store(true) }
func (ctx *armedEndCancellationContext) Err() error {
	if !ctx.armed.Load() || ctx.checks.Add(1) < 2 {
		return nil
	}
	if ctx.closed.CompareAndSwap(false, true) {
		close(ctx.done)
	}
	return context.Canceled
}

type callbackMarker struct {
	collected chan struct{}
}

func newCallbackMarker() (*callbackMarker, <-chan struct{}) {
	marker := &callbackMarker{collected: make(chan struct{})}
	runtime.SetFinalizer(marker, func(marker *callbackMarker) {
		close(marker.collected)
	})
	return marker, marker.collected
}

func callbackMarkerCollected(collected <-chan struct{}) bool {
	select {
	case <-collected:
		return true
	default:
		return false
	}
}

func waitForCallbackMarker(t *testing.T, collected <-chan struct{}) {
	t.Helper()
	for attempts := 0; attempts < 50 && !callbackMarkerCollected(collected); attempts++ {
		runtime.GC()
		runtime.Gosched()
	}
	if !callbackMarkerCollected(collected) {
		t.Fatal("callback payload remains retained after native lifecycle ended")
	}
}

func exerciseParseProgressPayload(t *testing.T, marker *callbackMarker) {
	t.Helper()
	parser := sitter.NewParser()
	if err := parser.SetLanguage(sitter.NewLanguage(python.Language())); err != nil {
		t.Fatal(err)
	}
	source := []byte(strings.Repeat("class Payload:\n    pass\n", 200))
	options := &sitter.ParseOptions{ProgressCallback: func(sitter.ParseState) bool {
		runtime.KeepAlive(marker)
		return false
	}}
	tree := parser.ParseWithOptions(func(offset int, _ sitter.Point) []byte {
		if offset < len(source) {
			return source[offset:]
		}
		return nil
	}, nil, options)
	if tree == nil {
		parser.Close()
		t.Fatal("parse with progress options returned nil")
	}
	tree.Close()
	parser.Close()
	runtime.KeepAlive(marker)
}

func exerciseQueryProgressPayloads(t *testing.T, markers []*callbackMarker) {
	t.Helper()
	parser := sitter.NewParser()
	if err := parser.SetLanguage(sitter.NewLanguage(python.Language())); err != nil {
		t.Fatal(err)
	}
	source := []byte("class Payload:\n    pass\n")
	tree := parser.Parse(source, nil)
	if tree == nil {
		parser.Close()
		t.Fatal("query payload fixture parse returned nil")
	}
	query, queryError := sitter.NewQuery(sitter.NewLanguage(python.Language()), "(class_definition) @item")
	if queryError != nil || query == nil {
		tree.Close()
		parser.Close()
		t.Fatalf("query payload fixture compilation: %v", queryError)
	}
	cursor := sitter.NewQueryCursor()

	_ = cursor.MatchesWithOptions(query, tree.RootNode(), source, queryOptionsForMarker(markers[0]))
	plainMatches := cursor.Matches(query, tree.RootNode(), source)
	for plainMatches.Next() != nil {
	}

	optionMatches := cursor.MatchesWithOptions(query, tree.RootNode(), source, queryOptionsForMarker(markers[1]))
	for optionMatches.Next() != nil {
	}
	plainCaptures := cursor.Captures(query, tree.RootNode(), source)
	for match, _ := plainCaptures.Next(); match != nil; match, _ = plainCaptures.Next() {
	}

	_ = cursor.MatchesWithOptions(query, tree.RootNode(), source, queryOptionsForMarker(markers[2]))
	cursor.Close()
	query.Close()
	tree.Close()
	parser.Close()
	for _, marker := range markers {
		runtime.KeepAlive(marker)
	}
}

func queryOptionsForMarker(marker *callbackMarker) sitter.QueryCursorOptions {
	return sitter.QueryCursorOptions{ProgressCallback: func(sitter.QueryCursorState) bool {
		runtime.KeepAlive(marker)
		return false
	}}
}

func exerciseQueryCursorDoubleClose() {
	parser := sitter.NewParser()
	_ = parser.SetLanguage(sitter.NewLanguage(python.Language()))
	source := []byte("class Close:\n    pass\n")
	tree := parser.Parse(source, nil)
	query, _ := sitter.NewQuery(sitter.NewLanguage(python.Language()), "(class_definition) @item")
	cursor := sitter.NewQueryCursor()
	_ = cursor.MatchesWithOptions(query, tree.RootNode(), source, sitter.QueryCursorOptions{
		ProgressCallback: func(sitter.QueryCursorState) bool { return false },
	})
	cursor.Close()
	cursor.Close()
	query.Close()
	tree.Close()
	parser.Close()
}

func TestTreeSitterLifecycle1000ParseSmoke(t *testing.T) {
	fixtures := []struct {
		path   string
		source string
	}{
		{"pkg/smoke.py", "class Smoke:\n    pass\n"},
		{"web/smoke.js", "class Smoke {}\n"},
		{"web/smoke.tsx", "const Smoke = () => <View />;\n"},
		{"src/smoke.rs", "struct Smoke;\n"},
	}
	before := runtime.NumGoroutine()
	for iteration := 0; iteration < 1000; iteration++ {
		fixture := fixtures[iteration%len(fixtures)]
		records, report := NewRegistry().Extract(stableFile(fixture.path, fixture.source))
		if len(records) == 0 || report.ParseFailures != 0 {
			t.Fatalf("iteration %d = records %#v report %#v", iteration, records, report)
		}
	}
	runtime.GC()
	deadline := time.Now().Add(2 * time.Second)
	for runtime.NumGoroutine() > before+2 && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if after := runtime.NumGoroutine(); after > before+2 {
		t.Fatalf("goroutines after lifecycle smoke = %d, before = %d", after, before)
	}
}

func TestTreeSitterConcurrentExtractorsUseIndependentParsers(t *testing.T) {
	fixtures := []struct {
		path   string
		source string
	}{
		{"pkg/concurrent.py", "class Concurrent:\n    pass\n"},
		{"web/concurrent.js", "class Concurrent {}\n"},
		{"web/concurrent.ts", "interface Concurrent {}\n"},
		{"src/concurrent.rs", "struct Concurrent;\n"},
	}
	want := make([][]model.Record, len(fixtures))
	for index, fixture := range fixtures {
		want[index], _ = NewRegistry().Extract(stableFile(fixture.path, fixture.source))
	}
	var wait sync.WaitGroup
	errors := make(chan error, 16)
	for worker := 0; worker < 16; worker++ {
		wait.Add(1)
		go func(worker int) {
			defer wait.Done()
			for iteration := 0; iteration < 25; iteration++ {
				index := (worker + iteration) % len(fixtures)
				fixture := fixtures[index]
				records, report := NewRegistry().Extract(stableFile(fixture.path, fixture.source))
				if report.ParseFailures != 0 || !reflect.DeepEqual(records, want[index]) {
					errors <- fmt.Errorf("worker %d iteration %d: records %#v report %#v", worker, iteration, records, report)
					return
				}
			}
		}(worker)
	}
	wait.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
}

type licenseInventory struct {
	SchemaVersion string          `json:"schema_version"`
	Modules       []licenseModule `json:"modules"`
}

type licenseModule struct {
	Module                    string          `json:"module"`
	Version                   string          `json:"version"`
	Direct                    bool            `json:"direct"`
	UpstreamLicense           string          `json:"upstream_license"`
	LicenseFile               string          `json:"license_file"`
	LicenseSHA256             string          `json:"license_sha256"`
	VendorTreeSHA256          string          `json:"vendor_tree_sha256"`
	UpstreamVendorTreeSHA256  string          `json:"upstream_vendor_tree_sha256"`
	VendorPatchFile           string          `json:"vendor_patch_file"`
	VendorPatchSHA256         string          `json:"vendor_patch_sha256"`
	PatchedFiles              []patchedFile   `json:"patched_files"`
	GoProxySum                string          `json:"go_proxy_sum"`
	GoModSum                  string          `json:"go_mod_sum"`
	OriginCommit              string          `json:"origin_commit"`
	OriginRef                 string          `json:"origin_ref"`
	UpstreamTagPresentAtAudit bool            `json:"upstream_tag_present_at_audit"`
	ImmutableResolution       string          `json:"immutable_resolution"`
	BundledNotices            []licenseNotice `json:"bundled_notices"`
}

type licenseNotice struct {
	License string `json:"license"`
	File    string `json:"file"`
	SHA256  string `json:"sha256"`
}

type patchedFile struct {
	File           string `json:"file"`
	UpstreamSHA256 string `json:"upstream_sha256"`
	PatchedSHA256  string `json:"patched_sha256"`
}

func TestLicenseInventoryMatchesModuleAndVendorGraph(t *testing.T) {
	moduleRoot := filepath.Clean(filepath.Join("..", ".."))
	raw, err := os.ReadFile(filepath.Join(moduleRoot, "licenses.json"))
	if err != nil {
		t.Fatal(err)
	}
	var inventory licenseInventory
	if err := json.Unmarshal(raw, &inventory); err != nil {
		t.Fatal(err)
	}
	if inventory.SchemaVersion != "1" {
		t.Fatalf("schema version = %q", inventory.SchemaVersion)
	}
	goModules := readGoModModules(t, filepath.Join(moduleRoot, "go.mod"))
	vendorModules := readVendorModules(t, filepath.Join(moduleRoot, "vendor", "modules.txt"))
	if !reflect.DeepEqual(goModules, vendorModules) {
		t.Fatalf("go.mod modules = %#v, vendor modules = %#v", goModules, vendorModules)
	}
	if len(inventory.Modules) != len(goModules) {
		t.Fatalf("license module count = %d, want %d", len(inventory.Modules), len(goModules))
	}
	expectedDirect := map[string]bool{
		"github.com/mattn/go-pointer":                   false,
		"github.com/tree-sitter/go-tree-sitter":         true,
		"github.com/tree-sitter/tree-sitter-javascript": true,
		"github.com/tree-sitter/tree-sitter-python":     true,
		"github.com/tree-sitter/tree-sitter-rust":       true,
		"github.com/tree-sitter/tree-sitter-typescript": true,
	}
	seen := make(map[string]bool, len(inventory.Modules))
	for index, module := range inventory.Modules {
		key := module.Module + "@" + module.Version
		if index != 0 && inventory.Modules[index-1].Module >= module.Module {
			t.Fatalf("license modules are not strictly sorted: %#v", inventory.Modules)
		}
		if seen[key] || goModules[module.Module] != module.Version {
			t.Fatalf("unversioned or duplicate license entry: %#v", module)
		}
		seen[key] = true
		if module.UpstreamLicense != "MIT" {
			t.Fatalf("module %s upstream license = %q", key, module.UpstreamLicense)
		}
		if module.Direct != expectedDirect[module.Module] {
			t.Fatalf("module %s direct = %t", key, module.Direct)
		}
		if module.Module == "github.com/tree-sitter/go-tree-sitter" {
			if module.Version != "v0.25.0" ||
				module.GoProxySum != "h1:sx6kcg8raRFCvc9BnXglke6axya12krCJF5xJ2sftRU=" ||
				module.GoModSum != "h1:r77ig7BikoZhHrrsjAnv8RqGti5rtSyvDHPzgTPsUuU=" ||
				module.OriginCommit != "adc13ffd8b2c0b01b878fda9f7c422ce0df5fad3" ||
				module.OriginRef != "refs/tags/v0.25.0" || module.UpstreamTagPresentAtAudit ||
				module.ImmutableResolution != "go-proxy-plus-sumdb" {
				t.Fatalf("runtime provenance controls = %#v", module)
			}
			if module.UpstreamVendorTreeSHA256 != "sha256:51596cd17c4eb630b074936fca6abdf6dfedcc5fa2d6d566b5a1b1535ce416c1" ||
				module.VendorPatchFile != "vendor/patches/go-tree-sitter-v0.25.0-callback-lifecycle.patch" ||
				module.VendorPatchSHA256 != "sha256:eda875a3f0c4e638ef1832fcdbab42576573a23fb86c21583aff9e83130e76a9" {
				t.Fatalf("runtime vendor patch controls = %#v", module)
			}
			wantPatchedFiles := []patchedFile{
				{
					File:           "vendor/github.com/tree-sitter/go-tree-sitter/parser.go",
					UpstreamSHA256: "sha256:489695115f7532c9e2a7b68cbbd335c9fea922ef77c9c664dc868ee381c65119",
					PatchedSHA256:  "sha256:19aeec45c0d7ad672524b8a943acca1befcc35b51a7362f4b11efa94c80ad48b",
				},
				{
					File:           "vendor/github.com/tree-sitter/go-tree-sitter/query.go",
					UpstreamSHA256: "sha256:8c0e950716d51344c3d02468a0b4067c0b0341ee4f0af28efe69682a7ebd3dcd",
					PatchedSHA256:  "sha256:0cf865430478f34b7c9baca7ad586bcfb3a5da928c2beab3391f6c558c3c22d4",
				},
			}
			if !reflect.DeepEqual(module.PatchedFiles, wantPatchedFiles) {
				t.Fatalf("runtime patched files = %#v", module.PatchedFiles)
			}
			patchBytes, err := os.ReadFile(filepath.Join(moduleRoot, filepath.FromSlash(module.VendorPatchFile)))
			if err != nil || digestBytes(patchBytes) != module.VendorPatchSHA256 {
				t.Fatalf("runtime vendor patch was not audited: %v", err)
			}
			for _, patched := range module.PatchedFiles {
				patchedBytes, err := os.ReadFile(filepath.Join(moduleRoot, filepath.FromSlash(patched.File)))
				if err != nil || digestBytes(patchedBytes) != patched.PatchedSHA256 {
					t.Fatalf("runtime patched file %s drifted: %v", patched.File, err)
				}
			}
			if len(module.BundledNotices) != 1 || module.BundledNotices[0] != (licenseNotice{
				License: "Unicode-3.0",
				File:    "vendor/github.com/tree-sitter/go-tree-sitter/src/unicode/LICENSE",
				SHA256:  "sha256:6a18c5fac70d7860b57f5b72b4e2c9a1ba6b3d2741eef7ff9767c5379364f10d",
			}) {
				t.Fatalf("runtime bundled notices = %#v", module.BundledNotices)
			}
			noticeBytes, err := os.ReadFile(filepath.Join(moduleRoot, filepath.FromSlash(module.BundledNotices[0].File)))
			if err != nil || digestBytes(noticeBytes) != module.BundledNotices[0].SHA256 || !strings.Contains(string(noticeBytes), "Unicode, Inc.") {
				t.Fatalf("runtime bundled Unicode notice was not audited: %v", err)
			}
		} else if len(module.BundledNotices) != 0 || module.UpstreamVendorTreeSHA256 != "" || module.VendorPatchFile != "" ||
			module.VendorPatchSHA256 != "" || len(module.PatchedFiles) != 0 {
			t.Fatalf("module %s has unexpected runtime patch metadata: %#v", key, module)
		}
		licensePath := filepath.Join(moduleRoot, filepath.FromSlash(module.LicenseFile))
		licenseBytes, err := os.ReadFile(licensePath)
		if err != nil {
			t.Fatalf("module %s license file: %v", key, err)
		}
		if !strings.Contains(string(licenseBytes), "The MIT License (MIT)") {
			t.Fatalf("module %s license text was not audited as MIT", key)
		}
		if digestBytes(licenseBytes) != module.LicenseSHA256 {
			t.Fatalf("module %s license digest drift", key)
		}
		vendorDirectory := filepath.Join(moduleRoot, "vendor", filepath.FromSlash(module.Module))
		if got := digestTree(t, vendorDirectory); got != module.VendorTreeSHA256 {
			t.Fatalf("module %s vendored source digest = %s, want %s", key, got, module.VendorTreeSHA256)
		}
	}
	for module := range goModules {
		if !seen[module+"@"+goModules[module]] {
			t.Fatalf("module %s@%s lacks license inventory", module, goModules[module])
		}
	}
	goSum, err := os.ReadFile(filepath.Join(moduleRoot, "go.sum"))
	if err != nil {
		t.Fatal(err)
	}
	for _, required := range []string{
		"github.com/tree-sitter/go-tree-sitter v0.25.0 h1:sx6kcg8raRFCvc9BnXglke6axya12krCJF5xJ2sftRU=",
		"github.com/tree-sitter/go-tree-sitter v0.25.0/go.mod h1:r77ig7BikoZhHrrsjAnv8RqGti5rtSyvDHPzgTPsUuU=",
	} {
		if !strings.Contains(string(goSum), required+"\n") {
			t.Fatalf("go.sum lacks %q", required)
		}
	}
	if strings.Contains(string(goSum), "github.com/tree-sitter/go-tree-sitter v0.24.0") {
		t.Fatal("go.sum retains rejected ABI14 runtime")
	}
}

func readGoModModules(t *testing.T, filename string) map[string]string {
	t.Helper()
	file, err := os.Open(filename)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	modules := make(map[string]string)
	inRequireBlock := false
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		switch {
		case line == "require (":
			inRequireBlock = true
			continue
		case inRequireBlock && line == ")":
			inRequireBlock = false
			continue
		case strings.HasPrefix(line, "require "):
			line = strings.TrimSpace(strings.TrimPrefix(line, "require "))
		case !inRequireBlock:
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 2 {
			modules[fields[0]] = fields[1]
		}
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return modules
}

func readVendorModules(t *testing.T, filename string) map[string]string {
	t.Helper()
	file, err := os.Open(filename)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	modules := make(map[string]string)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 3 && fields[0] == "#" {
			modules[fields[1]] = fields[2]
		}
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return modules
}

func digestBytes(contents []byte) string {
	digest := sha256.Sum256(contents)
	return "sha256:" + hex.EncodeToString(digest[:])
}

func digestTree(t *testing.T, root string) string {
	t.Helper()
	root, err := filepath.Abs(root)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	err = filepath.WalkDir(root, func(filename string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() {
			relative, err := filepath.Rel(root, filename)
			if err != nil {
				return err
			}
			names = append(names, filepath.ToSlash(relative))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(names)
	hash := sha256.New()
	for _, name := range names {
		contents, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(name)))
		if err != nil {
			t.Fatal(err)
		}
		writeLengthPrefixed(hash, []byte(name))
		writeLengthPrefixed(hash, contents)
	}
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func writeLengthPrefixed(hash interface{ Write([]byte) (int, error) }, value []byte) {
	var size [8]byte
	binary.BigEndian.PutUint64(size[:], uint64(len(value)))
	_, _ = hash.Write(size[:])
	_, _ = hash.Write(value)
}

func findRecord(t *testing.T, records []model.Record, qualified string) model.Record {
	t.Helper()
	for _, record := range records {
		if record.QualifiedName == qualified {
			return record
		}
	}
	t.Fatalf("record %q missing from %#v", qualified, records)
	return model.Record{}
}
