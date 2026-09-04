package render

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const testSHA = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func TestFitRanksNormalizesAndDoesNotMutateCaller(t *testing.T) {
	// This catches a renderer that leaks caller-owned slices/maps, leaves
	// duplicate warnings, or fails to derive ranks/counts from final findings.
	request := renderRequest(2000, 2)
	input := renderResult([]wire.Finding{renderFinding(2, "z", "β"), renderFinding(1, "a", "α"), renderFinding(3, "m", "γ")})
	input.Warnings = []string{"z-warning", "a-warning", "z-warning"}
	input.ParserVersions = map[string]string{"go": "go1"}

	fitted, err := Fit(request, input)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(fitted.Findings), 2; got != want {
		t.Fatalf("finding count = %d, want %d", got, want)
	}
	if got, want := fitted.Findings[0].Rank, 1; got != want {
		t.Fatalf("first rank = %d, want %d", got, want)
	}
	if got, want := fitted.Findings[1].Rank, 2; got != want {
		t.Fatalf("second rank = %d, want %d", got, want)
	}
	if got, want := strings.Join(fitted.Warnings, ","), "a-warning,z-warning"; got != want {
		t.Fatalf("warnings = %q, want %q", got, want)
	}
	if got, want := fitted.ReturnedCount, 2; got != want {
		t.Fatalf("returned = %d, want %d", got, want)
	}
	if got, want := fitted.OmittedCount, 1; got != want {
		t.Fatalf("omitted = %d, want %d", got, want)
	}
	if !fitted.Truncated {
		t.Fatal("expected truncated result")
	}
	if got := len(input.Findings); got != 3 {
		t.Fatalf("input findings mutated: %d", got)
	}
	if got := strings.Join(input.Warnings, ","); got != "z-warning,a-warning,z-warning" {
		t.Fatalf("input warnings mutated: %q", got)
	}
}

func TestFitRemovesOnlyOptionalTailUntilUnicodeBudgetFits(t *testing.T) {
	// This catches byte-oriented truncation and a renderer that trims a higher
	// ranked finding before the lowest-ranked optional one.
	request := renderRequest(2000, 64)
	findings := make([]wire.Finding, 0, 16)
	for index := 0; index < 16; index++ {
		name := string(rune('a' + index))
		findings = append(findings, renderFinding(index+1, name, strings.Repeat("🙂", 120)))
	}
	input := renderResult(findings)

	fitted, err := Fit(request, input)
	if err != nil {
		t.Fatal(err)
	}
	if len(fitted.Findings) >= len(input.Findings) || fitted.Findings[0].QualifiedName != "a" {
		t.Fatalf("unexpected retained findings: %#v", fitted.Findings)
	}
	if !utf8.ValidString(fitted.Findings[0].Preview) {
		t.Fatal("renderer produced invalid UTF-8")
	}
	if fitted.OutputCharacters > request.MaximumModelOutputCharacters {
		t.Fatalf("characters = %d, budget = %d", fitted.OutputCharacters, request.MaximumModelOutputCharacters)
	}
	if got, want := fitted.OutputCharacters, wire.OutputCharacters(fitted); got != want {
		t.Fatalf("output characters = %d, wire count = %d", got, want)
	}
}

func TestFitRejectsMandatoryResultBeyondBudget(t *testing.T) {
	// This catches a renderer that slices encoded JSON or emits an invalid
	// mandatory-only result when it cannot satisfy the caller's budget.
	request := renderRequest(1, 64)
	_, err := Fit(request, renderResult(nil))
	if !errors.Is(err, ErrUnrenderable) {
		t.Fatalf("Fit error = %v, want ErrUnrenderable", err)
	}
}

func TestFitKeepsTruncatedFromAnExhaustedSearch(t *testing.T) {
	request := renderRequest(12000, 8)
	input := renderResult([]wire.Finding{renderFinding(1, "a", "α")})
	input.Truncated = true
	fitted, err := Fit(request, input)
	if err != nil {
		t.Fatal(err)
	}
	if !fitted.Truncated || fitted.OmittedCount != 0 || fitted.ReturnedCount != 1 {
		t.Fatalf("fitted = truncated:%v omitted:%d returned:%d, want truncated with zero counted omissions", fitted.Truncated, fitted.OmittedCount, fitted.ReturnedCount)
	}
}

func TestFitReducesInitiallyOversizedJSONAndCharacterResults(t *testing.T) {
	// A final-only encoder used to reject this before the renderer could trim.
	findings := make([]wire.Finding, 0, 64)
	for index := 0; index < 64; index++ {
		findings = append(findings, renderFinding(index+1, string(rune('a'+index)), strings.Repeat("é", 256)))
	}
	result, err := Fit(renderRequest(12000, 64), renderResult(findings))
	if err != nil {
		t.Fatal(err)
	}
	if result.OutputCharacters > 12000 || len(result.Findings) >= 64 || !result.Truncated {
		t.Fatalf("unfitted result: %#v", result)
	}
	var encoded strings.Builder
	if err := wire.EncodeResult(&encoded, result); err != nil {
		t.Fatal(err)
	}
}

func TestFinalTransportRejectsOverTwelveThousandCharacters(t *testing.T) {
	findings := make([]wire.Finding, 0, 64)
	for index := 0; index < 64; index++ {
		findings = append(findings, renderFinding(index+1, string(rune('a'+index)), strings.Repeat("é", 256)))
	}
	result := renderResult(findings)
	result.ReturnedCount = len(findings)
	result.OutputCharacters = wire.OutputCharacters(result)
	if result.OutputCharacters < 19834 {
		t.Fatalf("fixture too small: %d", result.OutputCharacters)
	}
	var encoded strings.Builder
	if err := wire.EncodeResult(&encoded, result); !errors.Is(err, wire.ErrInvalidWire) {
		t.Fatalf("EncodeResult error = %v", err)
	}
}

func renderRequest(budget, maximumResults int) wire.Request {
	return wire.Request{MaximumModelOutputCharacters: budget, MaximumResults: maximumResults}
}

func renderResult(findings []wire.Finding) wire.Result {
	return wire.Result{
		SchemaVersion: "1", RequestIdentity: "request", Operation: wire.Estimate,
		Status: wire.Partial, ProviderIdentity: "taf-context", ProviderVersion: "0.1.0",
		RepositoryIdentity: testSHA, WorktreeIdentity: testSHA,
		CommittedHead: "0123456789abcdef0123456789abcdef01234567", DirtyOverlayFingerprint: testSHA,
		Freshness: "partial", ParserVersions: map[string]string{},
		Coverage: wire.Coverage{ExclusionReasonCounts: map[string]int{}}, Findings: findings,
		Warnings: []string{}, NextSafeAction: "build-index",
	}
}

func renderFinding(rank int, name, preview string) wire.Finding {
	digest := sha256.Sum256([]byte(name))
	return wire.Finding{
		Rank: rank, ResultIdentity: "sha256:" + hex.EncodeToString(digest[:]), Path: "pkg/" + name + ".go", StartLine: 1, EndLine: 1,
		Language: "go", RecordKind: "definition", SourceType: "source", QualifiedName: name,
		ExtractionMethod: "go-ast", EvidenceClass: "inferred", Preview: preview,
	}
}

// A directory table can be wider than any transport frame can carry — one row
// per directory, and a repository is free to have thousands of them. Such a
// result must still be an answer: the renderer folds the table's tail into the
// "*" row until the frame holds it, and it does that before it drops a single
// thing the caller asked for.
func TestFitFoldsAnOverviewTableTooWideForTheTransport(t *testing.T) {
	input := overviewRenderResult(2000, []wire.Finding{renderFinding(1, "a", "α"), renderFinding(2, "b", "β")})
	fitted, err := Fit(renderRequest(12000, 64), input)
	if err != nil {
		t.Fatalf("a table too wide for the transport must still answer: %v", err)
	}
	var encoded strings.Builder
	if err := wire.EncodeResult(&encoded, fitted); err != nil {
		t.Fatalf("the fitted result must encode: %v", err)
	}
	if len(fitted.Findings) != len(input.Findings) {
		t.Fatalf("findings = %d, want the %d the caller asked for: the table folds first", len(fitted.Findings), len(input.Findings))
	}
	rows := *fitted.Groups
	if len(rows) >= len(*input.Groups) || len(rows) < 2 {
		t.Fatalf("rows = %d, want a folded table of the 2000 rows handed in", len(rows))
	}
	folded := rows[len(rows)-1]
	if folded.PathPrefix != "*" || folded.Depth != 0 || folded.RepresentativeIdentity != nil {
		t.Fatalf("folded row = %#v", folded)
	}
	// Every row the table lost is counted exactly once, in the summary and in
	// the folded row, so a reader can still add the repository up.
	if got, want := fitted.Overview.OtherGroupCount, len(*input.Groups)-(len(rows)-1); got != want {
		t.Fatalf("other_group_count = %d, want %d", got, want)
	}
	if got, want := folded.FileCount, len(*input.Groups)-(len(rows)-1); got != want {
		t.Fatalf("folded file_count = %d, want the %d files of the folded rows", got, want)
	}
	if got := len(*input.Groups); got != 2000 || input.Overview.OtherGroupCount != 0 {
		t.Fatalf("the caller's own result was folded: %d rows, other_group_count %d", got, input.Overview.OtherGroupCount)
	}
}

// The folded row is the sum of the rows it speaks for, and reads like every
// other row of the table: its languages are merged and re-sorted, most files
// first and ties by name.
func TestFitFoldsRowsIntoOneSummedRow(t *testing.T) {
	input := overviewRenderResult(3, nil)
	(*input.Groups)[1].Languages = []wire.OverviewLanguage{{Language: "Go", FileCount: 1}}
	(*input.Groups)[2].Languages = []wire.OverviewLanguage{{Language: "Python", FileCount: 1}}
	fitted := input
	for folds := 0; folds < 2; folds++ {
		if !foldOverviewTail(&fitted) {
			t.Fatalf("fold %d refused a table of %d rows", folds, len(*fitted.Groups))
		}
	}
	rows := *fitted.Groups
	if len(rows) != 2 {
		t.Fatalf("rows = %#v, want one directory row and the folded row", rows)
	}
	folded := rows[1]
	if folded.PathPrefix != "*" || folded.FileCount != 2 || folded.DefinitionCount != 2 || folded.EntryPointCount != 2 {
		t.Fatalf("folded row = %#v", folded)
	}
	// "Go" and "Python" tie at one file each, so the tie-break by name is what
	// orders them; the counts of a repeated language are summed.
	want := []wire.OverviewLanguage{{Language: "Go", FileCount: 1}, {Language: "Python", FileCount: 1}}
	if got := folded.Languages; !reflect.DeepEqual(got, want) {
		t.Fatalf("folded languages = %#v, want %#v", got, want)
	}
	if fitted.Overview.OtherGroupCount != 2 {
		t.Fatalf("other_group_count = %d, want 2", fitted.Overview.OtherGroupCount)
	}
	// One directory row is the floor: a table of nothing but "*" would
	// describe no repository at all.
	if foldOverviewTail(&fitted) {
		t.Fatalf("folded past the last directory row: %#v", *fitted.Groups)
	}
}

// overviewRenderResult builds a schema-4 result with count plausible directory
// rows, so a test can say how wide a table it means rather than how it is
// spelled.
func overviewRenderResult(count int, findings []wire.Finding) wire.Result {
	result := renderResult(findings)
	result.SchemaVersion = "4"
	result.Operation = wire.RepositoryOverview
	index := testSHA
	result.IndexIdentity = &index
	rows := make([]wire.OverviewGroup, 0, count)
	for index := 0; index < count; index++ {
		identity := testSHA
		rows = append(rows, wire.OverviewGroup{
			PathPrefix: fmt.Sprintf("directory%04d/", index), Depth: 1, FileCount: 1,
			DefinitionCount: 1, EntryPointCount: 1,
			Languages:              []wire.OverviewLanguage{{Language: "Go", FileCount: 1}},
			RepresentativeIdentity: &identity,
		})
	}
	result.Groups = &rows
	result.Overview = &wire.OverviewSummary{Root: "", CountedFileCount: count, OtherGroupCount: 0}
	return result
}

// The wire admits at most MaximumOverviewGroups rows, so a table above that
// bound is folded down to it before anything is measured: a result the wire
// would refuse outright must still reach the caller as a shorter table.
func TestFitFoldsATableBeyondTheWireRowBound(t *testing.T) {
	rows := wire.MaximumOverviewGroups + 1
	fitted, err := Fit(renderRequest(12000, 64), overviewRenderResult(rows, []wire.Finding{renderFinding(1, "a", "α")}))
	if err != nil {
		t.Fatalf("a table beyond the row bound must still answer: %v", err)
	}
	if got := len(*fitted.Groups); got > wire.MaximumOverviewGroups {
		t.Fatalf("rows = %d, want at most the wire bound of %d", got, wire.MaximumOverviewGroups)
	}
	var encoded strings.Builder
	if err := wire.EncodeResult(&encoded, fitted); err != nil {
		t.Fatalf("the fitted result must encode: %v", err)
	}
	if got, want := fitted.Overview.OtherGroupCount, rows-(len(*fitted.Groups)-1); got != want {
		t.Fatalf("other_group_count = %d, want %d", got, want)
	}
}
