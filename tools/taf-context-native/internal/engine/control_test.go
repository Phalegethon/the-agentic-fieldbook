package engine

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"

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
		ProviderIdentity: "taf.native.level1", IndexIdentity: index, RequiredCapability: string(operation),
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
