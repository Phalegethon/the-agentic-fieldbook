package engine

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/render"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

type controlledSnippetContext struct {
	err      error
	canceled bool
}

func (ctx *controlledSnippetContext) Deadline() (time.Time, bool) { return time.Time{}, false }
func (ctx *controlledSnippetContext) Done() <-chan struct{}       { return nil }
func (ctx *controlledSnippetContext) Err() error {
	if ctx.canceled {
		return ctx.err
	}
	return nil
}
func (ctx *controlledSnippetContext) Value(any) any { return nil }
func (ctx *controlledSnippetContext) cancel()       { ctx.canceled = true }

// This catches a cancellation observed inside the initial exact-current
// inspection being converted into a representable result after no evidence
// could safely be produced.
func TestSourceSnippetsPropagatesCancellationFromInitialInspection(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	dependencies := ProductionDependencies()
	dependencies.Inspect = func(context.Context, *boundary.Roots) (store.Status, error) {
		return store.Status{}, context.Canceled
	}
	result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, engineSHA))
	if !errors.Is(executeErr, context.Canceled) || !reflect.DeepEqual(result, wire.Result{}) {
		t.Fatalf("result=%#v error=%v", result, executeErr)
	}
}

// This catches an output reduction that drops an oversized earlier requested
// preview but exposes a later one. Snippet output must be a request-order
// prefix, never a hole that changes which source evidence is presented.
func TestSourceSnippetsBudgetKeepsOnlyARequestOrderPrefix(t *testing.T) {
	content := "package fixture\n//" + strings.Repeat("x", 12000) + "\n// short\n"
	engine, repository, state, index := snippetFixture(t, content, []model.Record{
		snippetFixtureRecord(2, 2, "sha256:0000000000000000000000000000000000000000000000000000000000000001", content),
		snippetFixtureRecord(3, 3, "sha256:0000000000000000000000000000000000000000000000000000000000000002", content),
	})
	result, err := engine.Execute(context.Background(), snippetEnvelope(repository, state, index,
		"sha256:0000000000000000000000000000000000000000000000000000000000000001",
		"sha256:0000000000000000000000000000000000000000000000000000000000000002"))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Partial || result.OmittedCount != 2 || len(result.Findings) != 0 || result.NextSafeAction != "refine-query" {
		t.Fatalf("budget prefix = %#v", result)
	}
}

// This catches generic finding validation or snippet-local caps that discard
// an exact multiline preview even though it fits the request's 2,000-character
// output budget. The source newline is evidence and must remain a newline.
func TestSourceSnippetsRendersBoundedMultilinePreviewBeyondGenericTextLimit(t *testing.T) {
	content := "package fixture\n//" + strings.Repeat("é", 300) + "\n// exact-second-line\n"
	identity := "sha256:0000000000000000000000000000000000000000000000000000000000000003"
	engine, repository, state, index := snippetFixture(t, content, []model.Record{snippetFixtureRecord(2, 3, identity, content)})
	envelope := snippetEnvelope(repository, state, index, identity)
	envelope.Request.MaximumModelOutputCharacters = 2000
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil || result.Status != wire.Ready || len(result.Findings) != 1 {
		t.Fatalf("result=%#v error=%v", result, err)
	}
	want := "//" + strings.Repeat("é", 300) + "\n// exact-second-line"
	if result.Findings[0].Preview != want || result.OutputCharacters > 2000 {
		t.Fatalf("preview/output = %q/%d, want exact multiline <= 2000", result.Findings[0].Preview, result.OutputCharacters)
	}
}

func TestSourceSnippetsRendersAnExactEmptySourceLine(t *testing.T) {
	content := "package fixture\n\n// after\n"
	identity := "sha256:00000000000000000000000000000000000000000000000000000000000000e1"
	engine, repository, state, index := snippetFixture(t, content, []model.Record{snippetFixtureRecord(2, 2, identity, content)})
	result, err := engine.Execute(context.Background(), snippetEnvelope(repository, state, index, identity))
	if err != nil || result.Status != wire.Ready || len(result.Findings) != 1 || result.Findings[0].Preview != "" {
		t.Fatalf("result=%#v error=%v", result, err)
	}
	nonempty := result
	nonempty.Findings = append([]wire.Finding(nil), result.Findings...)
	nonempty.Findings[0].Preview = "x"
	if got, want := result.OutputCharacters, wire.OutputCharacters(nonempty)-1; got != want {
		t.Fatalf("empty-line output characters=%d, want %d", got, want)
	}
}

// This catches renderer-driven output reduction that changes the returned
// evidence set without changing the source-snippet status and safe action.
func TestSourceSnippetsRendererBudgetReductionIsPartialAndRefinable(t *testing.T) {
	content := "package fixture\n//" + strings.Repeat("x", 700) + "\n//" + strings.Repeat("y", 700) + "\n//" + strings.Repeat("z", 700) + "\n"
	identities := []string{
		"sha256:0000000000000000000000000000000000000000000000000000000000000004",
		"sha256:0000000000000000000000000000000000000000000000000000000000000005",
		"sha256:0000000000000000000000000000000000000000000000000000000000000006",
	}
	engine, repository, state, index := snippetFixture(t, content, []model.Record{
		snippetFixtureRecord(2, 2, identities[0], content),
		snippetFixtureRecord(3, 3, identities[1], content),
		snippetFixtureRecord(4, 4, identities[2], content),
	})
	envelope := snippetEnvelope(repository, state, index, identities...)
	envelope.Request.MaximumModelOutputCharacters = 2000
	result, err := engine.Execute(context.Background(), envelope)
	if err != nil || result.Status != wire.Partial || result.NextSafeAction != "refine-query" || result.OmittedCount == 0 || !result.Truncated || result.OutputCharacters > 2000 {
		t.Fatalf("result=%#v error=%v", result, err)
	}
}

func TestIndexedLinePreviewIsExactAndFailsClosed(t *testing.T) {
	for _, test := range []struct {
		name       string
		contents   []byte
		start, end int
		want       string
		wantError  bool
	}{
		{"crlf", []byte("secret-before\r\nαβ\r\nsecret-after\r\n"), 2, 2, "αβ", false},
		{"final-without-newline", []byte("before\nexact"), 2, 2, "exact", false},
		{"empty-line", []byte("before\n\nafter\n"), 2, 2, "", false},
		{"adjacent-secret-never-selected", []byte("SECRET-before\nexact\nSECRET-after\n"), 2, 2, "exact", false},
		{"bare-carriage-return", []byte("before\nbad\rvalue\n"), 2, 2, "", true},
		{"invalid-utf8", []byte("before\n\xff\n"), 2, 2, "", true},
		{"zero-range", []byte("line\n"), 0, 1, "", true},
		{"reversed-range", []byte("line\n"), 2, 1, "", true},
		{"past-eof", []byte("line\n"), 2, 2, "", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := indexedLinePreview(test.contents, test.start, test.end)
			if test.wantError {
				if err == nil {
					t.Fatalf("preview = %q, want error", got)
				}
				return
			}
			if err != nil || got != test.want {
				t.Fatalf("preview = %q, %v; want %q", got, err, test.want)
			}
		})
	}
}

func TestSourceSnippetsRejectsModifiedDeletedRenamedSymlinkAndOversizeSources(t *testing.T) {
	for _, mutate := range []struct {
		name  string
		apply func(t *testing.T, repository string)
	}{
		{"modified", func(t *testing.T, repository string) {
			if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Changed() {}\n"), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
		{"deleted", func(t *testing.T, repository string) {
			if err := os.Remove(filepath.Join(repository, "main.go")); err != nil {
				t.Fatal(err)
			}
		}},
		{"renamed", func(t *testing.T, repository string) {
			if err := os.Rename(filepath.Join(repository, "main.go"), filepath.Join(repository, "moved.go")); err != nil {
				t.Fatal(err)
			}
		}},
		{"symlink", func(t *testing.T, repository string) {
			if err := os.Remove(filepath.Join(repository, "main.go")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink("/dev/null", filepath.Join(repository, "main.go")); err != nil {
				t.Fatal(err)
			}
		}},
		{"oversize", func(t *testing.T, repository string) {
			if err := os.WriteFile(filepath.Join(repository, "main.go"), append([]byte("package sample\n//"), make([]byte, productionLimits().MaximumSourceFileBytes)...), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(mutate.name, func(t *testing.T) {
			repository, state := controlRoots(t)
			base := New(ProductionDependencies())
			built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
			if err != nil {
				t.Fatal(err)
			}
			record := snippetRecord(t, repository, state, *built.IndexIdentity)
			mutate.apply(t, repository)
			result, executeErr := base.Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
			if executeErr != nil || result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 {
				t.Fatalf("result=%#v error=%v", result, executeErr)
			}
		})
	}
}

func TestSnippetGroupValidationRejectsInferredConflictingAndUnknownRecordsBeforeOpening(t *testing.T) {
	content := "package fixture\nfunc Exact() {}\n"
	known := snippetFixtureRecord(2, 2, "sha256:0000000000000000000000000000000000000000000000000000000000000001", content)
	inferred := known
	inferred.EvidenceClass = model.Inferred
	if _, err := resolveSnippetGroups([]model.Record{inferred}, []string{inferred.Identity}); err == nil {
		t.Fatal("inferred record resolved")
	}
	conflict := known
	conflict.Identity = "sha256:0000000000000000000000000000000000000000000000000000000000000002"
	conflict.SourceDigest = engineSHA
	if _, err := resolveSnippetGroups([]model.Record{known, conflict}, []string{known.Identity, conflict.Identity}); err == nil {
		t.Fatal("conflicting path digests resolved")
	}
	if _, err := resolveSnippetGroups([]model.Record{known}, []string{"sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}); err == nil {
		t.Fatal("unknown identity resolved")
	}
}

func TestSourceSnippetsRejectsCurrentReplacementAfterSourceRead(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)
	dependencies := ProductionDependencies()
	inspect := dependencies.Inspect
	inspections, opens := 0, 0
	dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
		status, inspectErr := inspect(ctx, roots)
		inspections++
		if inspections == 2 && inspectErr == nil {
			status.IndexIdentity = engineSHA
		}
		return status, inspectErr
	}
	open := dependencies.OpenFile
	dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opens++
		return open(roots, relative, maximum)
	}
	result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
	if executeErr != nil || result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 || opens != 1 || inspections != 2 {
		t.Fatalf("result=%#v error=%v opens=%d inspections=%d", result, executeErr, opens, inspections)
	}
}

func TestSourceSnippetsRejectsSameIndexDifferentGenerationAfterSourceRead(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)
	dependencies := ProductionDependencies()
	inspect := dependencies.Inspect
	inspections, opens := 0, 0
	dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
		status, inspectErr := inspect(ctx, roots)
		inspections++
		if inspections == 2 && inspectErr == nil {
			status.GenerationIdentity = engineSHA
			status.Manifest.GenerationIdentity = engineSHA
		}
		return status, inspectErr
	}
	open := dependencies.OpenFile
	dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opens++
		return open(roots, relative, maximum)
	}
	result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
	if executeErr != nil || result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 || opens != 1 || inspections != 2 {
		t.Fatalf("result=%#v error=%v opens=%d inspections=%d", result, executeErr, opens, inspections)
	}
}

// This catches a replacement whose index payload is unchanged but whose
// immutable generation/manifest identity is different before source I/O.
func TestSourceSnippetsRejectsSameIndexDifferentGenerationBeforeOpen(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		if loadErr == nil {
			snapshot.Manifest.GenerationIdentity = engineSHA
		}
		return snapshot, loadErr
	}
	opens := 0
	open := dependencies.OpenFile
	dependencies.OpenFile = func(roots *boundary.Roots, path string, maximum int64) (boundary.StableFile, error) {
		opens++
		return open(roots, path, maximum)
	}
	result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
	if executeErr != nil || result.Status != wire.Stale || result.Freshness != "structurally-stale" || len(result.Findings) != 0 || opens != 0 {
		t.Fatalf("result=%#v error=%v opens=%d", result, executeErr, opens)
	}
}

// This catches a context cancellation which races with a non-context loader
// failure being downgraded into a regular stale/error result.
func TestSourceSnippetsCancellationDominatesLoaderFailure(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	dependencies := ProductionDependencies()
	dependencies.Load = func(context.Context, *boundary.Roots, string) (store.Snapshot, error) {
		cancel()
		return store.Snapshot{}, store.ErrStoreCorrupt
	}
	result, executeErr := New(dependencies).Execute(ctx, snippetEnvelope(repository, state, built.IndexIdentity, engineSHA))
	if !errors.Is(executeErr, context.Canceled) || !reflect.DeepEqual(result, wire.Result{}) {
		t.Fatalf("result=%#v error=%v", result, executeErr)
	}
}

func TestSourceSnippetsCancellationDominatesFit(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	dependencies := ProductionDependencies()
	dependencies.Fit = func(_ context.Context, request wire.Request, result wire.Result) (wire.Result, error) {
		cancel()
		return render.Fit(request, result)
	}
	result, executeErr := New(dependencies).Execute(ctx, snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
	if !errors.Is(executeErr, context.Canceled) || !reflect.DeepEqual(result, wire.Result{}) {
		t.Fatalf("result=%#v error=%v", result, executeErr)
	}
}

func TestSourceSnippetsCancellationDominatesRefit(t *testing.T) {
	content := "package sample\n//" + strings.Repeat("x", 700) + "\n//" + strings.Repeat("y", 700) + "\n//" + strings.Repeat("z", 700) + "\n"
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	identities := []string{"sha256:" + strings.Repeat("0", 63) + "7", "sha256:" + strings.Repeat("0", 63) + "8", "sha256:" + strings.Repeat("0", 63) + "9"}
	records := []model.Record{snippetFixtureRecord(2, 2, identities[0], content), snippetFixtureRecord(3, 3, identities[1], content), snippetFixtureRecord(4, 4, identities[2], content)}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, identity)
		if loadErr == nil {
			snapshot.Records = records
		}
		return snapshot, loadErr
	}
	calls := 0
	dependencies.Fit = func(_ context.Context, request wire.Request, result wire.Result) (wire.Result, error) {
		calls++
		if calls == 2 {
			cancel()
		}
		return render.Fit(request, result)
	}
	envelope := snippetEnvelope(repository, state, built.IndexIdentity, identities...)
	envelope.Request.MaximumModelOutputCharacters = 2000
	result, executeErr := New(dependencies).Execute(ctx, envelope)
	if !errors.Is(executeErr, context.Canceled) || !reflect.DeepEqual(result, wire.Result{}) || calls != 2 {
		t.Fatalf("result=%#v error=%v calls=%d", result, executeErr, calls)
	}
}

func TestSourceSnippetsContextErrorsDominateEveryDependencySeam(t *testing.T) {
	for _, contextErr := range []error{context.Canceled, context.DeadlineExceeded} {
		for _, seam := range []string{"initial-inspect", "load", "open", "final-inspect"} {
			t.Run(seam+"-"+contextErr.Error(), func(t *testing.T) {
				repository, state := controlRoots(t)
				base := New(ProductionDependencies())
				built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
				if err != nil {
					t.Fatal(err)
				}
				record := snippetRecord(t, repository, state, *built.IndexIdentity)
				ctx := &controlledSnippetContext{err: contextErr}
				dependencies := ProductionDependencies()
				nonContextErr := errors.New("seam failure")
				switch seam {
				case "initial-inspect":
					dependencies.Inspect = func(context.Context, *boundary.Roots) (store.Status, error) {
						ctx.cancel()
						return store.Status{}, nonContextErr
					}
				case "load":
					dependencies.Load = func(context.Context, *boundary.Roots, string) (store.Snapshot, error) {
						ctx.cancel()
						return store.Snapshot{}, nonContextErr
					}
				case "open":
					dependencies.OpenFile = func(*boundary.Roots, string, int64) (boundary.StableFile, error) {
						ctx.cancel()
						return boundary.StableFile{}, nonContextErr
					}
				case "final-inspect":
					inspect := dependencies.Inspect
					calls := 0
					dependencies.Inspect = func(callCtx context.Context, roots *boundary.Roots) (store.Status, error) {
						status, inspectErr := inspect(callCtx, roots)
						calls++
						if calls == 2 {
							ctx.cancel()
							return status, nonContextErr
						}
						return status, inspectErr
					}
				}
				result, executeErr := New(dependencies).Execute(ctx, snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
				if !errors.Is(executeErr, contextErr) || !reflect.DeepEqual(result, wire.Result{}) {
					t.Fatalf("result=%#v error=%v want=%v", result, executeErr, contextErr)
				}
			})
		}
	}
}

func TestSourceSnippetsContextErrorsDominateFitAndRefitFailures(t *testing.T) {
	for _, contextErr := range []error{context.Canceled, context.DeadlineExceeded} {
		for _, seam := range []string{"fit", "refit"} {
			t.Run(seam+"-"+contextErr.Error(), func(t *testing.T) {
				content := "package sample\n//" + strings.Repeat("x", 700) + "\n//" + strings.Repeat("y", 700) + "\n//" + strings.Repeat("z", 700) + "\n"
				repository, state := controlRoots(t)
				if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte(content), 0o600); err != nil {
					t.Fatal(err)
				}
				base := New(ProductionDependencies())
				built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
				if err != nil {
					t.Fatal(err)
				}
				identities := []string{"sha256:" + strings.Repeat("0", 63) + "a", "sha256:" + strings.Repeat("0", 63) + "b", "sha256:" + strings.Repeat("0", 63) + "c"}
				records := []model.Record{snippetFixtureRecord(2, 2, identities[0], content), snippetFixtureRecord(3, 3, identities[1], content), snippetFixtureRecord(4, 4, identities[2], content)}
				ctx := &controlledSnippetContext{err: contextErr}
				dependencies := ProductionDependencies()
				load := dependencies.Load
				dependencies.Load = func(loadCtx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
					snapshot, loadErr := load(loadCtx, roots, identity)
					if loadErr == nil {
						snapshot.Records = records
					}
					return snapshot, loadErr
				}
				calls := 0
				dependencies.Fit = func(_ context.Context, request wire.Request, result wire.Result) (wire.Result, error) {
					calls++
					if seam == "fit" || calls == 2 {
						ctx.cancel()
						return wire.Result{}, errors.New("fit failure")
					}
					return render.Fit(request, result)
				}
				envelope := snippetEnvelope(repository, state, built.IndexIdentity, identities...)
				envelope.Request.MaximumModelOutputCharacters = 2000
				result, executeErr := New(dependencies).Execute(ctx, envelope)
				if !errors.Is(executeErr, contextErr) || !reflect.DeepEqual(result, wire.Result{}) || (seam == "refit" && calls != 2) {
					t.Fatalf("result=%#v error=%v calls=%d want=%v", result, executeErr, calls, contextErr)
				}
			})
		}
	}
}

func TestSourceSnippetsOpensEachRequestedPathOnceAndNoOtherPath(t *testing.T) {
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte("package sample\nfunc Main() {}\nfunc Other() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	records := snippetRecords(t, repository, state, *built.IndexIdentity)
	if len(records) < 2 {
		t.Fatalf("records=%#v", records)
	}
	identities := sortedSnippetIDs(records[0].Identity, records[1].Identity)
	dependencies := ProductionDependencies()
	opened := map[string]int{}
	open := dependencies.OpenFile
	dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
		opened[relative]++
		return open(roots, relative, maximum)
	}
	result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, identities...))
	if executeErr != nil || result.Status != wire.Ready || len(result.Findings) != 2 || !reflect.DeepEqual(opened, map[string]int{"main.go": 1}) {
		t.Fatalf("result=%#v error=%v opened=%#v", result, executeErr, opened)
	}
}

func TestSourceSnippetsSupports64IdentitiesDeterministically(t *testing.T) {
	var source strings.Builder
	source.WriteString("package fixture\n")
	identities := make([]string, 64)
	records := make([]model.Record, 64)
	for index := range records {
		source.WriteString("// line\n")
		identities[index] = "sha256:" + fmt.Sprintf("%064x", index+100)
		records[index] = snippetFixtureRecord(index+2, index+2, identities[index], source.String())
	}
	content := source.String()
	for index := range records {
		records[index].SourceDigest = snippetFixtureRecord(1, 1, identities[index], content).SourceDigest
	}
	engine, repository, state, index := snippetFixture(t, content, records)
	envelope := snippetEnvelope(repository, state, index, identities...)
	var first wire.Result
	for repeat := 0; repeat < 50; repeat++ {
		result, err := engine.Execute(context.Background(), envelope)
		if err != nil {
			t.Fatal(err)
		}
		if repeat == 0 {
			first = result
		} else if !reflect.DeepEqual(first, result) {
			t.Fatal("non-deterministic 64-identity result")
		}
	}
	if first.OmittedCount+len(first.Findings) != 64 || first.OutputCharacters > 12000 {
		t.Fatalf("result=%#v", first)
	}
}

func TestSourceSnippetsPropagatesCancellationAfterLoadAndOpen(t *testing.T) {
	for _, point := range []string{"after-load", "after-open"} {
		t.Run(point, func(t *testing.T) {
			repository, state := controlRoots(t)
			base := New(ProductionDependencies())
			built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
			if err != nil {
				t.Fatal(err)
			}
			record := snippetRecord(t, repository, state, *built.IndexIdentity)
			dependencies := ProductionDependencies()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if point == "after-load" {
				load := dependencies.Load
				dependencies.Load = func(ctx context.Context, roots *boundary.Roots, identity string) (store.Snapshot, error) {
					snapshot, loadErr := load(ctx, roots, identity)
					cancel()
					return snapshot, loadErr
				}
			} else {
				open := dependencies.OpenFile
				dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
					file, openErr := open(roots, relative, maximum)
					cancel()
					return file, openErr
				}
			}
			result, executeErr := New(dependencies).Execute(ctx, snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
			if !errors.Is(executeErr, context.Canceled) || !reflect.DeepEqual(result, wire.Result{}) {
				t.Fatalf("result=%#v error=%v", result, executeErr)
			}
		})
	}
}

func TestSourceSnippetsRejectsWrongIndexBindingParserAndPolicyBeforeOpen(t *testing.T) {
	for _, mutation := range []struct {
		name  string
		apply func(*store.Status)
	}{
		{"wrong-index", func(status *store.Status) { status.IndexIdentity = engineSHA }},
		{"worktree", func(status *store.Status) { status.Manifest.Binding.WorktreeIdentity = testSHA2 }},
		{"parser", func(status *store.Status) { status.Manifest.ParserIdentities = map[string]string{"go": "changed"} }},
		{"policy", func(status *store.Status) { status.Manifest.InclusionPolicyIdentity = engineSHA }},
	} {
		t.Run(mutation.name, func(t *testing.T) {
			repository, state := controlRoots(t)
			base := New(ProductionDependencies())
			built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
			if err != nil {
				t.Fatal(err)
			}
			record := snippetRecord(t, repository, state, *built.IndexIdentity)
			dependencies := ProductionDependencies()
			inspect := dependencies.Inspect
			dependencies.Inspect = func(ctx context.Context, roots *boundary.Roots) (store.Status, error) {
				status, inspectErr := inspect(ctx, roots)
				if inspectErr == nil {
					mutation.apply(&status)
				}
				return status, inspectErr
			}
			opens := 0
			open := dependencies.OpenFile
			dependencies.OpenFile = func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
				opens++
				return open(roots, relative, maximum)
			}
			result, executeErr := New(dependencies).Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
			if executeErr != nil || result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 || opens != 0 {
				t.Fatalf("result=%#v error=%v opens=%d", result, executeErr, opens)
			}
		})
	}
}

func FuzzSnippetLinePreviewFailsClosed(f *testing.F) {
	f.Add([]byte("before\r\nexact\r\nafter\r\n"), uint8(2), uint8(2))
	f.Add([]byte("before\n\xff\nafter\n"), uint8(2), uint8(2))
	f.Fuzz(func(t *testing.T, contents []byte, start, end uint8) {
		preview, err := indexedLinePreview(contents, int(start), int(end))
		if err != nil {
			return
		}
		if !utf8.ValidString(preview) || strings.ContainsAny(preview, "\x00\r") || utf8.RuneCountInString(preview) > maximumSnippetPreviewCharacters {
			t.Fatalf("unsafe preview %q", preview)
		}
	})
}

func snippetFixture(t *testing.T, content string, records []model.Record) (*Engine, string, string, *string) {
	t.Helper()
	repository, state := controlRoots(t)
	if err := os.WriteFile(filepath.Join(repository, "main.go"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	// Install the fixture's valid source records only in the immutable loader
	// result; the real current manifest and binding still gate every request.
	dependencies := ProductionDependencies()
	load := dependencies.Load
	dependencies.Load = func(ctx context.Context, roots *boundary.Roots, expected string) (store.Snapshot, error) {
		snapshot, loadErr := load(ctx, roots, expected)
		if loadErr == nil {
			snapshot.Records = append([]model.Record(nil), records...)
		}
		return snapshot, loadErr
	}
	return New(dependencies), repository, state, built.IndexIdentity
}

func snippetFixtureRecord(start, end int, identity, content string) model.Record {
	digest := sha256.Sum256([]byte(content))
	return model.Record{Identity: identity, Path: "main.go", StartLine: start, EndLine: end, Language: "go", RecordKind: model.Definition, SourceType: "source", QualifiedName: "fixture", ExtractionMethod: "go-ast", EvidenceClass: model.Verified, SourceDigest: "sha256:" + hex.EncodeToString(digest[:])}
}

// This catches dispatch that leaves the frozen source-snippets capability
// unsupported, rather than returning source bytes verified against its index.
func TestSourceSnippetsReturnsExactVerifiedIndexedRange(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)

	result, err := base.Execute(context.Background(), snippetEnvelope(repository, state, built.IndexIdentity, record.Identity))
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != wire.Ready || result.Freshness != "exact" || result.NextSafeAction != "use-index" || len(result.Findings) != 1 {
		t.Fatalf("snippet result = %#v", result)
	}
	finding := result.Findings[0]
	if finding.ResultIdentity != record.Identity || finding.Preview != "func Main() {}" || finding.StartLine != 2 || finding.EndLine != 2 {
		t.Fatalf("finding = %#v, record = %#v", finding, record)
	}
}

// This catches an unknown requested identity reaching the repository open
// seam. Identity resolution must happen before every source read.
func TestSourceSnippetsRejectsUnknownOrMixedIdentityBeforeSourceOpen(t *testing.T) {
	repository, state := controlRoots(t)
	base := New(ProductionDependencies())
	built, err := base.Execute(context.Background(), controlEnvelope(wire.Build, repository, state, nil))
	if err != nil {
		t.Fatal(err)
	}
	record := snippetRecord(t, repository, state, *built.IndexIdentity)
	unknown := "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

	for _, identities := range [][]string{{unknown}, sortedSnippetIDs(record.Identity, unknown)} {
		t.Run("identity-count-"+string(rune('0'+len(identities))), func(t *testing.T) {
			dependencies := ProductionDependencies()
			opens := 0
			open := dependencies.OpenFile
			dependencies.OpenFile = func(roots *boundary.Roots, path string, maximum int64) (boundary.StableFile, error) {
				opens++
				return open(roots, path, maximum)
			}
			envelope := snippetEnvelope(repository, state, built.IndexIdentity, identities...)
			result, executeErr := New(dependencies).Execute(context.Background(), envelope)
			if executeErr != nil {
				t.Fatal(executeErr)
			}
			if result.Status != wire.Stale || result.Freshness != "structurally-stale" || result.NextSafeAction != "update-index" || len(result.Findings) != 0 || opens != 0 {
				t.Fatalf("result = %#v opens=%d", result, opens)
			}
		})
	}
}

func snippetRecord(t *testing.T, repository, state, index string) model.Record {
	t.Helper()
	roots, err := boundary.ValidateRoots(controlEnvelope(wire.StatusOperation, repository, state, &index))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	snapshot, err := store.LoadContext(context.Background(), &roots, index)
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range snapshot.Records {
		if record.EvidenceClass == model.Verified && record.Path == "main.go" && record.StartLine == 2 {
			return record
		}
	}
	t.Fatalf("no verified Main record in %#v", snapshot.Records)
	return model.Record{}
}

func snippetRecords(t *testing.T, repository, state, index string) []model.Record {
	t.Helper()
	roots, err := boundary.ValidateRoots(controlEnvelope(wire.StatusOperation, repository, state, &index))
	if err != nil {
		t.Fatal(err)
	}
	defer roots.Close()
	snapshot, err := store.LoadContext(context.Background(), &roots, index)
	if err != nil {
		t.Fatal(err)
	}
	var records []model.Record
	for _, record := range snapshot.Records {
		if record.EvidenceClass == model.Verified && record.Path == "main.go" {
			records = append(records, record)
		}
	}
	return records
}

func snippetEnvelope(repository, state string, index *string, identities ...string) wire.Envelope {
	envelope := controlEnvelope(wire.SourceSnippets, repository, state, index)
	envelope.Request.ResultIdentities = append([]string(nil), identities...)
	return envelope
}

func sortedSnippetIDs(values ...string) []string {
	output := append([]string(nil), values...)
	sort.Strings(output)
	return output
}
