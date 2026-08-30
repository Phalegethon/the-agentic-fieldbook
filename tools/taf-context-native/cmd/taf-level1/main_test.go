package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const cliSHA = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func TestRunEncodesOneResultForOneFramedRequest(t *testing.T) {
	input := framedEnvelope(t)
	var stdout, stderr bytes.Buffer
	exit := runWithExecutor(strings.NewReader(input), &stdout, &stderr, func(_ context.Context, envelope wire.Envelope) (wire.Result, error) {
		return resultFor(envelope), nil
	})
	if exit != 0 || stderr.Len() != 0 {
		t.Fatalf("exit=%d stderr=%q", exit, stderr.String())
	}
	if bytes.Count(stdout.Bytes(), []byte("\n")) != 1 || !bytes.HasSuffix(stdout.Bytes(), []byte("\n")) {
		t.Fatalf("stdout framing = %q", stdout.Bytes())
	}
	var result wire.Result
	if err := json.Unmarshal(bytes.TrimSuffix(stdout.Bytes(), []byte("\n")), &result); err != nil || result.RequestIdentity != "native-cli-001" {
		t.Fatalf("result framing/value = %#v, %v", result, err)
	}
}

func TestServerEncodesOneResultPerFramedRequest(t *testing.T) {
	input := framedEnvelope(t) + strings.Replace(framedEnvelope(t), "native-cli-001", "native-cli-002", 1)
	var stdout, stderr bytes.Buffer
	calls := 0
	exit := runServerWithExecutor(strings.NewReader(input), &stdout, &stderr, func(_ context.Context, envelope wire.Envelope) (wire.Result, error) {
		calls++
		return resultFor(envelope), nil
	})
	if exit != 0 || calls != 2 || stderr.Len() != 0 || bytes.Count(stdout.Bytes(), []byte("\n")) != 2 {
		t.Fatalf("exit=%d calls=%d stdout=%q stderr=%q", exit, calls, stdout.String(), stderr.String())
	}
	lines := bytes.Split(bytes.TrimSpace(stdout.Bytes()), []byte("\n"))
	for index, line := range lines {
		var result wire.Result
		if err := json.Unmarshal(line, &result); err != nil || result.RequestIdentity != fmt.Sprintf("native-cli-%03d", index+1) {
			t.Fatalf("result %d = %#v, %v", index, result, err)
		}
	}
}

func TestTrustedUpdateCounterChannelIsCanonicalBoundedAndSourceFree(t *testing.T) {
	var output bytes.Buffer
	err := writeUpdateCounters(&output, model.WorkCounters{
		ChangedPaths: 1, ParsedRepositoryFiles: 1, OpenedRepositoryFiles: 3,
		ReadRepositoryBytes: 123,
	})
	if err != nil {
		t.Fatal(err)
	}
	want := "__TAF_LEVEL1_UPDATE_COUNTERS_V1__={\"changed_paths\":1,\"opened_repository_files\":3,\"parsed_repository_files\":1,\"read_repository_bytes\":123}\n"
	if output.String() != want || output.Len() > policy.ProductionLimits().MaximumStderrBytes || strings.Contains(output.String(), "/") {
		t.Fatalf("counter observation = %q", output.String())
	}
}

func TestProductionRunnerRejectsUnknownArguments(t *testing.T) {
	var stdout, stderr bytes.Buffer
	exit := runProduction(strings.NewReader(framedEnvelope(t)), &stdout, &stderr, []string{"--unknown"})
	if exit != 2 || stdout.Len() != 0 || stderr.String() != "invalid-native-level1-request\n" {
		t.Fatalf("exit=%d stdout=%q stderr=%q", exit, stdout.String(), stderr.String())
	}
}

func TestProductionFlagsAllowServerWithTrustedUpdateCounters(t *testing.T) {
	server, counters, ok := productionFlags([]string{"--serve", "--observe-update-counters"})
	if !ok || !server || !counters {
		t.Fatalf("server=%v counters=%v ok=%v", server, counters, ok)
	}
	for _, arguments := range [][]string{{"--serve", "--serve"}, {"--observe-update-counters", "--unknown"}} {
		if _, _, valid := productionFlags(arguments); valid {
			t.Fatalf("accepted invalid arguments %q", arguments)
		}
	}
}

func TestProductionServerSignalsReadinessBeforeServingRequests(t *testing.T) {
	var stdout, stderr bytes.Buffer
	exit := runProduction(strings.NewReader(""), &stdout, &stderr, []string{"--serve"})
	if exit != 0 || stdout.Len() != 0 {
		t.Fatalf("exit=%d stdout=%q", exit, stdout.String())
	}
	if stderr.String() != "__TAF_LEVEL1_SERVER_READY_V1__\n" {
		t.Fatalf("readiness = %q", stderr.String())
	}
}

func TestRunRejectsUnsafeInputWithStableBoundedReason(t *testing.T) {
	cases := map[string]string{
		"missing newline":    strings.TrimSuffix(framedEnvelope(t), "\n"),
		"multiple frames":    framedEnvelope(t) + framedEnvelope(t),
		"oversized stdin":    strings.Repeat("x", policy.ProductionLimits().MaximumWireBytes+1) + "\n",
		"malformed envelope": "{not-json}\n",
	}
	for name, input := range cases {
		t.Run(name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			exit := run(strings.NewReader(input), &stdout, &stderr)
			if exit != 2 || stdout.Len() != 0 || stderr.String() != "invalid-native-level1-request\n" {
				t.Fatalf("exit=%d stdout=%q stderr=%q", exit, stdout.String(), stderr.String())
			}
		})
	}
}

func TestRunRejectsTheLegacyNativeProviderIdentity(t *testing.T) {
	input := strings.Replace(framedEnvelope(t), "taf-context", "taf.native.level1", 1)
	var stdout, stderr bytes.Buffer
	called := false
	exit := runWithExecutor(strings.NewReader(input), &stdout, &stderr, func(_ context.Context, envelope wire.Envelope) (wire.Result, error) {
		called = true
		return resultFor(envelope), nil
	})
	if exit != 2 || called || stdout.Len() != 0 || stderr.String() != "invalid-native-level1-request\n" {
		t.Fatalf("exit=%d called=%v stdout=%q stderr=%q", exit, called, stdout.String(), stderr.String())
	}
}

func TestRunContainsPanicsAndOutputFailures(t *testing.T) {
	t.Run("panic", func(t *testing.T) {
		var stdout, stderr bytes.Buffer
		exit := runWithExecutor(strings.NewReader(framedEnvelope(t)), &stdout, &stderr, func(context.Context, wire.Envelope) (wire.Result, error) {
			panic("/private/source/never-disclose")
		})
		if exit != 3 || stdout.Len() != 0 || stderr.String() != "native-level1-internal-error\n" || strings.Contains(stderr.String(), "/private/") {
			t.Fatalf("exit=%d stdout=%q stderr=%q", exit, stdout.String(), stderr.String())
		}
	})
	t.Run("broken stdout", func(t *testing.T) {
		var stderr bytes.Buffer
		exit := runWithExecutor(strings.NewReader(framedEnvelope(t)), errWriter{err: errors.New("/private/output")}, &stderr, func(_ context.Context, envelope wire.Envelope) (wire.Result, error) {
			return resultFor(envelope), nil
		})
		if exit != 3 || stderr.String() != "native-level1-output-error\n" || strings.Contains(stderr.String(), "/private/") {
			t.Fatalf("exit=%d stderr=%q", exit, stderr.String())
		}
	})
}

type errWriter struct{ err error }

func (writer errWriter) Write([]byte) (int, error) { return 0, writer.err }

func framedEnvelope(t *testing.T) string {
	t.Helper()
	root := filepath.VolumeName(os.TempDir()) + string(filepath.Separator)
	encoded, err := json.Marshal(wire.Envelope{
		Phase: "estimate", RepositoryRoot: filepath.Join(root, "repository"), StateRoot: filepath.Join(root, "state"),
		Request: wire.Request{
			SchemaVersion: "1", RequestIdentity: "native-cli-001", ConsumerIdentity: "taf.work-recovery",
			Operation: wire.Estimate, RepositoryIdentity: cliSHA, WorktreeIdentity: cliSHA,
			CommittedHead: "0123456789abcdef0123456789abcdef01234567", DirtyOverlayFingerprint: cliSHA,
			ProviderIdentity: "taf-context", RequiredCapability: "estimate", MinimumFreshness: "exact",
			Filters:        wire.Filters{PathPrefixes: []string{}, Languages: []string{}, SymbolKinds: []string{}, SourceTypes: []string{}},
			MaximumResults: 1, MaximumModelOutputCharacters: 2000, ResultIdentities: []string{},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded) + "\n"
}

func resultFor(envelope wire.Envelope) wire.Result {
	result := wire.Result{
		SchemaVersion: "1", RequestIdentity: envelope.Request.RequestIdentity, Operation: envelope.Request.Operation,
		Status: wire.Partial, ProviderIdentity: "taf-context", ProviderVersion: "0.1.0",
		RepositoryIdentity: envelope.Request.RepositoryIdentity, WorktreeIdentity: envelope.Request.WorktreeIdentity,
		CommittedHead: envelope.Request.CommittedHead, DirtyOverlayFingerprint: envelope.Request.DirtyOverlayFingerprint,
		Freshness: "partial", ParserVersions: map[string]string{}, Findings: []wire.Finding{}, Warnings: []string{},
		Coverage: wire.Coverage{ExclusionReasonCounts: map[string]int{}}, NextSafeAction: "build-index",
	}
	result.OutputCharacters = wire.OutputCharacters(result)
	return result
}

var _ io.Writer = errWriter{}
