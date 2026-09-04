package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/extract"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestBoundedWarningOverflowIsDeterministic(t *testing.T) {
	warnings := make([]string, 65)
	for index := range warnings {
		warnings[index] = fmt.Sprintf("warning-%03d", 64-index)
	}
	got := appendBoundedWarnings(nil, warnings...)
	if len(got) != 64 || got[63] != "warning-limit" || got[0] != "warning-000" || got[62] != "warning-062" {
		t.Fatalf("warnings = %#v", got)
	}
	for repeat := 0; repeat < 20; repeat++ {
		if !reflect.DeepEqual(got, appendBoundedWarnings(nil, warnings...)) {
			t.Fatal("warning retention changed")
		}
	}
	chunks := appendBoundedWarnings(nil, warnings[:17]...)
	chunks = appendBoundedWarnings(chunks, warnings[17:43]...)
	chunks = appendBoundedWarnings(chunks, warnings[43:]...)
	if !reflect.DeepEqual(got, chunks) {
		t.Fatalf("batch merge = %#v, want %#v", chunks, got)
	}
	reversed := append([]string(nil), warnings...)
	for left, right := 0, len(reversed)-1; left < right; left, right = left+1, right-1 {
		reversed[left], reversed[right] = reversed[right], reversed[left]
	}
	if !reflect.DeepEqual(got, appendBoundedWarnings(nil, reversed...)) {
		t.Fatal("reverse permutation changed retention")
	}
}

const engineSHA = "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"

func TestEstimateIsPartialReadOnlyAndDeterministic(t *testing.T) {
	// This catches accidental state creation or complete eligible-source reads
	// during the estimate-only control path.
	repository, state := controlRoots(t)
	envelope := controlEnvelope(wire.Estimate, repository, state, nil)
	engine := New(ProductionDependencies())

	first, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	second, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if first.Status != wire.Partial || first.Freshness != "partial" || first.Coverage.ParseFailureCount != 0 || first.NextSafeAction != "build-index" {
		t.Fatalf("estimate = %#v", first)
	}
	if len(first.Findings) != 0 || !hasWarning(first.Warnings, "coverage-estimated-not-parsed") {
		t.Fatalf("estimate findings/warnings = %#v", first)
	}
	if _, err := os.Stat(state); !os.IsNotExist(err) {
		t.Fatalf("estimate created state: %v", err)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("estimate is not deterministic: %#v != %#v", first, second)
	}
}

func TestBuildThenStatusAndMetricsAreExactAndDoNotLeakFindings(t *testing.T) {
	// This catches a build that does not publish a usable immutable generation,
	// and control reads that overclaim readiness or return record/source data.
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	if built.Status != wire.Ready || built.Freshness != "exact" || built.IndexIdentity == nil || built.NextSafeAction != "use-index" {
		t.Fatalf("build = %#v", built)
	}
	for _, operation := range []wire.Operation{wire.StatusOperation, wire.Metrics} {
		result, err := engine.Execute(context.Background(), controlEnvelope(operation, repository, state, built.IndexIdentity))
		if err != nil {
			t.Fatalf("%s: %v", operation, err)
		}
		if result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" || len(result.Findings) != 0 {
			t.Fatalf("%s result = %#v", operation, result)
		}
	}
}

// TestBuildPublishesFormat3ManifestAtEngineVersion pins the versions a store
// format v4 index is published under: manifest format "3", engine 0.4.0, and
// the same engine version reported back as the provider version.
func TestBuildPublishesFormat3ManifestAtEngineVersion(t *testing.T) {
	repository, state := controlRoots(t)
	dependencies := ProductionDependencies()
	build := dependencies.Build
	var published model.Manifest
	dependencies.Build = func(ctx context.Context, roots *boundary.Roots, manifest model.Manifest, records []model.Record) (store.Snapshot, error) {
		published = manifest
		return build(ctx, roots, manifest, records)
	}
	built, err := New(dependencies).Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	if built.Status != wire.Ready {
		t.Fatalf("build = %#v", built)
	}
	if published.FormatVersion != "3" || published.EngineVersion != "0.4.0" {
		t.Fatalf("manifest format = %q engine = %q, want \"3\" and \"0.4.0\"", published.FormatVersion, published.EngineVersion)
	}
	if built.ProviderVersion != "0.4.0" {
		t.Fatalf("provider version = %q, want 0.4.0", built.ProviderVersion)
	}
}

func TestBoundedExtractionPublishesQueryablePartialIndex(t *testing.T) {
	// This catches deterministic extractor limits being treated as parse
	// failures that make an otherwise valid persisted index unusable forever.
	repository, state := controlRoots(t)
	wide := "{"
	for index := 0; index <= 64; index++ {
		if index != 0 {
			wide += ","
		}
		wide += fmt.Sprintf("%q:0", fmt.Sprintf("key-%02d", index))
	}
	wide += "}"
	if err := os.WriteFile(filepath.Join(repository, "wide.json"), []byte(wide), 0o600); err != nil {
		t.Fatal(err)
	}

	engine := New(ProductionDependencies())
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	if built.Status != wire.Partial || built.Freshness != "exact" || built.IndexIdentity == nil || built.NextSafeAction != "use-index" {
		t.Fatalf("build = %#v", built)
	}
	if built.Coverage.ParseFailureCount != 0 || !hasWarning(built.Warnings, "json-collection-limit") {
		t.Fatalf("build coverage/warnings = %#v", built)
	}

	status, err := engine.Execute(context.Background(), controlEnvelope(wire.StatusOperation, repository, state, built.IndexIdentity))
	if err != nil {
		t.Fatal(err)
	}
	if status.Status != wire.Partial || status.Freshness != "exact" || status.NextSafeAction != "use-index" || !hasWarning(status.Warnings, "partial-index-coverage") {
		t.Fatalf("status = %#v", status)
	}

	queryText := "Main"
	query := controlEnvelope(wire.SearchSymbols, repository, state, built.IndexIdentity)
	query.Request.Query = &queryText
	queried, err := engine.Execute(context.Background(), query)
	if err != nil {
		t.Fatal(err)
	}
	if queried.Status != wire.Partial || queried.Freshness != "exact" || queried.NextSafeAction == "rebuild-index" || len(queried.Findings) == 0 || !hasWarning(queried.Warnings, "json-collection-limit") {
		t.Fatalf("query = %#v", queried)
	}
}

func TestBuildExtractsIndependentFilesConcurrently(t *testing.T) {
	repository, state := controlRoots(t)
	for index := 0; index < 24; index++ {
		path := filepath.Join(repository, fmt.Sprintf("parallel-%02d.go", index))
		if err := os.WriteFile(path, []byte(fmt.Sprintf("package sample\nfunc Parallel%d() {}\n", index)), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	previous := runtime.GOMAXPROCS(4)
	defer runtime.GOMAXPROCS(previous)
	dependencies := ProductionDependencies()
	extractFile := dependencies.Extract
	var active, maximum int32
	dependencies.Extract = func(ctx context.Context, file boundary.StableFile) ([]model.Record, extract.Report) {
		current := atomic.AddInt32(&active, 1)
		for observed := atomic.LoadInt32(&maximum); current > observed && !atomic.CompareAndSwapInt32(&maximum, observed, current); observed = atomic.LoadInt32(&maximum) {
		}
		time.Sleep(5 * time.Millisecond)
		records, report := extractFile(ctx, file)
		atomic.AddInt32(&active, -1)
		return records, report
	}
	result, err := New(dependencies).Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil || result.Status != wire.Ready {
		t.Fatalf("build = %#v, %v", result, err)
	}
	if maximum < 2 {
		t.Fatalf("maximum concurrent extractions = %d, want at least 2", maximum)
	}
}

func TestBuildReusesStableBodiesAlreadyReadByBuildInventory(t *testing.T) {
	repository, state := controlRoots(t)
	dependencies := ProductionDependencies()
	open := dependencies.OpenFile
	var extractionOpens int32
	dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		atomic.AddInt32(&extractionOpens, 1)
		return open(roots, relative, maximum)
	}
	result, err := New(dependencies).Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil || result.Status != wire.Ready {
		t.Fatalf("build = %#v, %v", result, err)
	}
	if extractionOpens != 0 {
		t.Fatalf("extraction reopened %d inventory-stable bodies", extractionOpens)
	}
}

func TestStatusAndMetricsRefuseMismatchedWorktreeAndAbsentState(t *testing.T) {
	// This catches control reads that create/repair missing state or report an
	// exact index after the request's worktree binding changes.
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	missing := controlEnvelope(wire.StatusOperation, repository, state, testPtr(engineSHA))
	result, err := engine.Execute(context.Background(), missing)
	if err != nil {
		t.Fatal(err)
	}
	if result.Freshness != "unusable" || result.NextSafeAction != "build-index" || len(result.Findings) != 0 {
		t.Fatalf("absent state = %#v", result)
	}
	built, err := engine.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	mismatch := controlEnvelope(wire.Metrics, repository, state, built.IndexIdentity)
	mismatch.Request.WorktreeIdentity = "sha256:bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a"
	result, err = engine.Execute(context.Background(), mismatch)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
		t.Fatalf("mismatched worktree = %#v", result)
	}
}

func TestUpdateWithoutChangeDocumentReturnsBoundedStaleResult(t *testing.T) {
	// A missing control document must be stale/rebuild rather than a fallback
	// build or an unsupported response now that update is implemented.
	repository, state := controlRoots(t)
	engine := New(ProductionDependencies())
	envelope := controlEnvelope(wire.Update, repository, state, testPtr(engineSHA))
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "rebuild-index" || len(result.Findings) != 0 {
		t.Fatalf("missing document = %#v", result)
	}
}

func TestStatusAndQueriesPeekWhileMetricsInspects(t *testing.T) {
	// This catches the query path or status regressing to the raw structural
	// validator, and metrics silently losing it.
	repository, state := controlRoots(t)
	built, err := New(ProductionDependencies()).Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		operation    wire.Operation
		query        bool
		wantPeeks    int
		wantInspects int
	}{
		{wire.StatusOperation, false, 1, 0},
		{wire.Metrics, false, 0, 1},
		{wire.RepositoryMap, false, 1, 0},
		{wire.SearchSymbols, true, 1, 0},
		{wire.SearchDocs, true, 1, 0},
	}
	for _, test := range tests {
		t.Run(string(test.operation), func(t *testing.T) {
			dependencies := ProductionDependencies()
			peek, inspect := dependencies.Peek, dependencies.Inspect
			peeks, inspects := 0, 0
			dependencies.Peek = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
				peeks++
				return peek(ctx, roots)
			}
			dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
				inspects++
				return inspect(ctx, roots)
			}
			envelope := controlEnvelope(test.operation, repository, state, built.IndexIdentity)
			if test.query {
				query := "main"
				envelope.Request.Query = &query
			}
			result, executeErr := New(dependencies).Execute(context.Background(), envelope)
			if executeErr != nil || result.Status != wire.Ready || result.Freshness != "exact" {
				t.Fatalf("result = %#v, %v", result, executeErr)
			}
			if peeks != test.wantPeeks || inspects != test.wantInspects {
				t.Fatalf("peeks=%d inspects=%d, want %d and %d", peeks, inspects, test.wantPeeks, test.wantInspects)
			}
		})
	}
}

func TestEngineWithoutPeekIsNotReady(t *testing.T) {
	dependencies := ProductionDependencies()
	dependencies.Peek = nil
	_, err := New(dependencies).Execute(context.Background(), wire.Envelope{})
	if !errors.Is(err, ErrDependencies) {
		t.Fatalf("error = %v, want ErrDependencies", err)
	}
}

func controlRoots(t *testing.T) (string, string) {
	t.Helper()
	base := t.TempDir()
	repository := filepath.Join(base, "repository")
	if err := os.Mkdir(repository, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Main() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return repository, filepath.Join(base, "state")
}

func controlEnvelope(operation wire.Operation, repository, state string, index *string) wire.Envelope {
	return wire.Envelope{Phase: "query", RepositoryRoot: repository, StateRoot: state, Request: wire.Request{
		SchemaVersion: "1", RequestIdentity: "request", ConsumerIdentity: "consumer", Operation: operation,
		RepositoryIdentity: engineSHA, WorktreeIdentity: engineSHA,
		CommittedHead: "0123456789abcdef0123456789abcdef01234567", DirtyOverlayFingerprint: engineSHA,
		ProviderIdentity: "taf-context", IndexIdentity: index, RequiredCapability: string(operation),
		MinimumFreshness: "exact", Filters: wire.Filters{}, MaximumResults: 64, MaximumModelOutputCharacters: 12000,
	}}
}

func testPtr(value string) *string { return &value }

func hasWarning(warnings []string, want string) bool {
	for _, warning := range warnings {
		if warning == want {
			return true
		}
	}
	return false
}

// The adapter template advertises capabilities, and a capability is an
// operation name: the broker sets `required_capability` to the operation it
// asks for and the wire layer refuses any other value
// (wire.ErrRequiredCapability, decode.go), so an advertised capability that is
// not an operation could never be required, and an operation that is not
// advertised could never be reached. The rule is therefore exact rather than a
// subset: the capability list is the whole frozen operation vocabulary,
// sorted, with no duplicate and no extra entry. The lifecycle operations
// (build, estimate, metrics, status, update) belong to that vocabulary and so
// appear in the list too; `supported_phases` is a separate, coarser list and is
// not part of this rule.
func TestAdapterTemplateAdvertisesExactlyTheOperationsTheEngineServes(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "adapter", "manifest.template.json"))
	if err != nil {
		t.Fatal(err)
	}
	var template struct {
		AdapterVersion  string   `json:"adapter_version"`
		ProviderVersion string   `json:"provider_version"`
		Capabilities    []string `json:"capabilities"`
	}
	if err := json.Unmarshal(raw, &template); err != nil {
		t.Fatal(err)
	}
	want := make([]string, 0, len(wire.Operations()))
	for _, operation := range wire.Operations() {
		want = append(want, string(operation))
	}
	sort.Strings(want)
	if !reflect.DeepEqual(template.Capabilities, want) {
		t.Fatalf("template capabilities = %#v, want %#v", template.Capabilities, want)
	}
	if template.AdapterVersion != engineVersion || template.ProviderVersion != engineVersion {
		t.Fatalf("template versions = %q/%q, want %q for both", template.AdapterVersion, template.ProviderVersion, engineVersion)
	}
}

// The engine build does not decide whether a stored index still answers: the
// index format, the extraction policies and the bindings do. 0.4.0 added
// `changed-symbols` without touching the format, so an index 0.3.0 wrote stays
// exact and is used as it is. A freshness rule that compared the engine
// version instead would force a rebuild nothing needs on every release.
func TestFreshnessForKeepsAnIndexAnOlderEngineVersionWrote(t *testing.T) {
	index := "sha256:" + strings.Repeat("b", 64)
	request := controlEnvelope(wire.ChangedSymbols, "", "", &index).Request
	request.SchemaVersion = "3"
	parsers := ProductionDependencies().ParserIDs()
	manifest := model.Manifest{
		FormatVersion:           "3",
		EngineVersion:           "0.3.0",
		InclusionPolicyIdentity: currentInclusionPolicyIdentity(),
		ExclusionPolicyIdentity: currentExclusionPolicyIdentity(),
		ParserIdentities:        parsers,
		Binding: model.Binding{
			RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity,
			CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint,
		},
	}
	// Without this the case would go vacuous the moment the engine version
	// caught up with the manifest's.
	if manifest.EngineVersion == engineVersion {
		t.Fatalf("manifest engine version %q is the current one, so nothing is stale here", manifest.EngineVersion)
	}
	if freshness, action := freshnessFor(request, manifest, index, parsers); freshness != "exact" || action != "use-index" {
		t.Fatalf("index written by %s = %s/%s, want exact/use-index", manifest.EngineVersion, freshness, action)
	}
}

func TestFreshnessForRejectsAnIndexBuiltUnderTheOlderExtractionPolicy(t *testing.T) {
	index := "sha256:" + strings.Repeat("a", 64)
	request := controlEnvelope(wire.SearchSymbols, "", "", &index).Request
	parsers := ProductionDependencies().ParserIDs()
	manifest := model.Manifest{
		InclusionPolicyIdentity: currentInclusionPolicyIdentity(),
		ExclusionPolicyIdentity: currentExclusionPolicyIdentity(),
		ParserIdentities:        parsers,
		Binding: model.Binding{
			RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity,
			CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint,
		},
	}
	if freshness, action := freshnessFor(request, manifest, index, parsers); freshness != "exact" || action != "use-index" {
		t.Fatalf("current policy = %s/%s", freshness, action)
	}
	inclusion, _ := inventory.PolicyIdentities()
	manifest.InclusionPolicyIdentity = hashParts([]string{"taf-level1-inclusion-composite-v1", inclusion, "extract-v1 path=4096 components=256 warnings=64"})
	if freshness, action := freshnessFor(request, manifest, index, parsers); freshness != "structurally-stale" || action != "rebuild-index" {
		t.Fatalf("older policy = %s/%s, want structurally-stale/rebuild-index", freshness, action)
	}
}
