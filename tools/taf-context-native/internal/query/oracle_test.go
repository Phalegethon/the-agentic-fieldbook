package query

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/render"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// oracleOutcome is the frozen shape of one oracle query's result: the ordered
// result identities plus the budget counters that the "results frozen" rule
// (spec 4b) requires every later optimisation to keep byte-identical.
type oracleOutcome struct {
	Name              string   `json:"name"`
	Identities        []string `json:"identities"`
	Omitted           int      `json:"omitted"`
	Partial           bool     `json:"partial"`
	ConsideredRecords int      `json:"considered_records"`
	TermVisits        int      `json:"term_visits"`
}

// oracleOutcomes runs every oracle query against snapshot and captures the
// frozen outcome shape for each. It is shared by the always-on 20k fixture
// test and the opt-in 100k bench comparison.
func oracleOutcomes(snapshot store.Snapshot) []oracleOutcome {
	limits := policy.ProductionLimits()
	outcomes := make([]oracleOutcome, 0, len(oracleQueries()))
	for _, item := range oracleQueries() {
		request := item.request()
		var response Response
		if item.Operation == wire.RepositoryMap {
			response = RepositoryMap(snapshot, request, limits)
		} else {
			response = Search(snapshot, request, limits)
		}
		identities := make([]string, 0, len(response.Records))
		for _, record := range response.Records {
			identities = append(identities, record.Identity)
		}
		outcomes = append(outcomes, oracleOutcome{item.Name, identities, response.Omitted, response.Partial, response.Counters.ConsideredRecords, response.TermVisits})
	}
	return outcomes
}

// TestSearchOracleAt20kIsByteIdentical freezes the exact search and map
// results (identities in order, plus the budget counters) that the twenty
// oracle queries produce against a synthetic 19,992-record snapshot. The
// fixture was generated once from the unchanged search code; a byte-for-byte
// mismatch means a later optimisation changed observable behaviour, which
// the "results frozen" rule (spec 4b) forbids without owner sign-off.
func TestSearchOracleAt20kIsByteIdentical(t *testing.T) {
	fixture := filepath.Join("testdata", "query-oracle-20k.json")
	got, err := json.MarshalIndent(oracleOutcomes(syntheticSnapshot(syntheticRecords(392, 25, 25))), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	if os.Getenv("TAF_UPDATE_ORACLE") == "1" {
		if err := os.WriteFile(fixture, got, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("rewrote %s", fixture)
	}
	want, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("search results changed; run with TAF_UPDATE_ORACLE=1 only for an intentional, owner-approved result change.\n%s", unifiedDiffHint(want, got))
	}
}

// unifiedDiffHint reports the first line at which want and got diverge, with
// up to ten lines of context from each side, to make a byte-identical
// fixture mismatch diagnosable without reaching for an external diff tool.
func unifiedDiffHint(want, got []byte) string {
	wantLines := strings.Split(string(want), "\n")
	gotLines := strings.Split(string(got), "\n")
	limit := len(wantLines)
	if len(gotLines) < limit {
		limit = len(gotLines)
	}
	firstDiff := -1
	for index := 0; index < limit; index++ {
		if wantLines[index] != gotLines[index] {
			firstDiff = index
			break
		}
	}
	if firstDiff == -1 {
		firstDiff = limit
	}
	var out strings.Builder
	fmt.Fprintf(&out, "first differing line: %d\n", firstDiff+1)
	end := firstDiff + 10
	fmt.Fprintf(&out, "--- want\n")
	for index := firstDiff; index < end && index < len(wantLines); index++ {
		fmt.Fprintf(&out, "%d: %s\n", index+1, wantLines[index])
	}
	fmt.Fprintf(&out, "--- got\n")
	for index := firstDiff; index < end && index < len(gotLines); index++ {
		fmt.Fprintf(&out, "%d: %s\n", index+1, gotLines[index])
	}
	return out.String()
}

// TestBenchSearchAtScale times Search and RepositoryMap plus render.Fit for
// every oracle query against a synthetic 100k-record snapshot. It is
// opt-in and prints nothing unless TAF_ENGINE_BENCH=1.
func TestBenchSearchAtScale(t *testing.T) {
	if os.Getenv("TAF_ENGINE_BENCH") != "1" {
		t.Skip("set TAF_ENGINE_BENCH=1 to print Search and Fit timings")
	}
	records := 100000
	if raw := os.Getenv("TAF_BENCH_RECORDS"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 51 {
			t.Fatalf("TAF_BENCH_RECORDS=%q", raw)
		}
		records = parsed
	}
	modules := records / 51 // 25 functions + 25 classes + 1 module record
	started := time.Now()
	snapshot := syntheticSnapshot(syntheticRecords(modules, 25, 25))
	t.Logf("records=%d index_build=%s", len(snapshot.Records), time.Since(started))
	limits := policy.ProductionLimits()
	// Any well-formed identity; Fit never dereferences the index. The oracle
	// requests below carry none, so the wire.Result literal needs a fixed,
	// correctly shaped value to satisfy wire.validateResult.
	index := "sha256:" + hexRepeat("d")
	for _, item := range oracleQueries() {
		request := item.request()
		search := make([]time.Duration, 0, 5)
		fit := make([]time.Duration, 0, 5)
		var response Response
		for range 5 {
			began := time.Now()
			if item.Operation == wire.RepositoryMap {
				response = RepositoryMap(snapshot, request, limits)
			} else {
				response = Search(snapshot, request, limits)
			}
			search = append(search, time.Since(began))
			result := wire.Result{SchemaVersion: "1", RequestIdentity: request.RequestIdentity, Operation: request.Operation, Status: wire.Ready, ProviderIdentity: "taf-context", ProviderVersion: "0.1.1", IndexIdentity: &index, RepositoryIdentity: request.RepositoryIdentity, WorktreeIdentity: request.WorktreeIdentity, CommittedHead: request.CommittedHead, DirtyOverlayFingerprint: request.DirtyOverlayFingerprint, Freshness: "exact", ParserVersions: map[string]string{}, Coverage: wire.Coverage{ExclusionReasonCounts: map[string]int{}}, Findings: findingsOf(response.Records), OmittedCount: response.Omitted, Truncated: response.Partial || response.Omitted > 0, Warnings: []string{}, NextSafeAction: "use-index"}
			began = time.Now()
			if _, err := render.Fit(request, result); err != nil {
				t.Fatal(err)
			}
			fit = append(fit, time.Since(began))
		}
		slices.Sort(search)
		slices.Sort(fit)
		t.Logf("%-22s search=%s fit=%s records=%d omitted=%d partial=%v considered=%d terms=%d", item.Name, search[2], fit[2], len(response.Records), response.Omitted, response.Partial, response.Counters.ConsideredRecords, response.TermVisits)
	}
	if records == 100000 {
		fixture := filepath.Join("testdata", "query-oracle-100k.json")
		got, err := json.MarshalIndent(oracleOutcomes(snapshot), "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, '\n')
		if os.Getenv("TAF_UPDATE_ORACLE") == "1" {
			if err := os.WriteFile(fixture, got, 0o644); err != nil {
				t.Fatal(err)
			}
			t.Logf("rewrote %s", fixture)
		}
		if want, err := os.ReadFile(fixture); err == nil {
			if !bytes.Equal(got, want) {
				t.Fatalf("search results changed at 100k; run with TAF_UPDATE_ORACLE=1 TAF_BENCH_RECORDS=100000 only for an intentional, owner-approved result change.\n%s", unifiedDiffHint(want, got))
			}
		} else if !os.IsNotExist(err) {
			t.Fatal(err)
		}
	}
}

// findingsOf mirrors internal/engine.findings; it is copied here rather than
// imported to avoid a query -> engine -> query import cycle (engine imports
// query for its own search/map operations).
func findingsOf(records []model.Record) []wire.Finding {
	output := make([]wire.Finding, len(records))
	for index, record := range records {
		output[index] = wire.Finding{Rank: index + 1, ResultIdentity: record.Identity, Path: record.Path, StartLine: record.StartLine, EndLine: record.EndLine, Language: record.Language, RecordKind: string(record.RecordKind), SourceType: record.SourceType, QualifiedName: record.QualifiedName, ExtractionMethod: record.ExtractionMethod, EvidenceClass: string(record.EvidenceClass), Preview: record.Preview}
	}
	return output
}
