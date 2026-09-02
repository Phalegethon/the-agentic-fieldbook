package render

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
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
