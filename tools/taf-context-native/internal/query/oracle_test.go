package query

import (
	"os"
	"slices"
	"strconv"
	"testing"
	"time"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/render"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

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
