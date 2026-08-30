package integration

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const integrationSHA = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

func TestNativeExecutableRunsEveryFrozenOperationInSequence(t *testing.T) {
	binary := buildNativeBinary(t)
	repository := filepath.Join(t.TempDir(), "repository")
	state := filepath.Join(t.TempDir(), "state")
	if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repository, "sample.go"), []byte("package sample\n\nfunc NativeContract() {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	estimate := invoke(t, binary, envelope(wire.Estimate, repository, state, nil, nil))
	if estimate.Status != wire.Partial {
		t.Fatalf("estimate status = %s", estimate.Status)
	}
	built := invoke(t, binary, envelope(wire.Build, repository, state, nil, nil))
	if built.Status != wire.Ready || built.IndexIdentity == nil {
		t.Fatalf("build = %#v", built)
	}
	index := built.IndexIdentity
	var symbolResult wire.Result
	for _, operation := range []wire.Operation{wire.StatusOperation, wire.Metrics, wire.RepositoryMap, wire.SearchSymbols, wire.SearchDocs} {
		result := invoke(t, binary, envelope(operation, repository, state, index, nil))
		if result.Operation != operation || result.Status == wire.Error || result.Status == wire.Unsupported {
			t.Fatalf("%s = %#v", operation, result)
		}
		if operation == wire.SearchSymbols {
			symbolResult = result
		}
	}
	if len(symbolResult.Findings) == 0 {
		t.Fatalf("search-symbols returned no identity for source-snippets: %#v", symbolResult)
	}
	identities := []string{symbolResult.Findings[0].ResultIdentity}
	snippets := invoke(t, binary, envelope(wire.SourceSnippets, repository, state, index, identities))
	if snippets.Operation != wire.SourceSnippets || snippets.Status == wire.Error || snippets.Status == wire.Unsupported {
		t.Fatalf("source-snippets = %#v", snippets)
	}

	update := invoke(t, binary, envelope(wire.Update, repository, state, index, nil))
	if update.Operation != wire.Update || update.Status != wire.Stale || update.NextSafeAction != "rebuild-index" {
		t.Fatalf("update without trusted delta = %#v", update)
	}
}

func TestNativeExecutableTurnsAClosedStdoutIntoTheSafeOutputFailure(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows has no SIGPIPE disposition")
	}
	binary := buildNativeBinary(t)
	repository := filepath.Join(t.TempDir(), "repository")
	state := filepath.Join(t.TempDir(), "state")
	if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := reader.Close(); err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	encoded, err := json.Marshal(envelope(wire.Estimate, repository, state, nil, nil))
	if err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	command := exec.Command(binary)
	command.Stdin = bytes.NewReader(append(encoded, '\n'))
	command.Stdout = writer
	command.Stderr = &stderr
	err = command.Run()
	exited, ok := err.(*exec.ExitError)
	if !ok || exited.ExitCode() != 3 || stderr.String() != "native-level1-output-error\n" {
		t.Fatalf("error=%v stderr=%q", err, stderr.String())
	}
}

func buildNativeBinary(t *testing.T) string {
	t.Helper()
	name := "taf-level1"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	binary := filepath.Join(t.TempDir(), name)
	command := exec.Command("go", "build", "-mod=vendor", "-trimpath", "-o", binary, "../cmd/taf-level1")
	command.Dir = "."
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("build native executable: %v\n%s", err, output)
	}
	return binary
}

func invoke(t *testing.T, binary string, request wire.Envelope) wire.Result {
	t.Helper()
	encoded, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(binary)
	command.Stdin = bytes.NewReader(append(encoded, '\n'))
	stdout, err := command.Output()
	if err != nil {
		if exited, ok := err.(*exec.ExitError); ok {
			t.Fatalf("native %s: %v stderr=%q", request.Request.Operation, err, exited.Stderr)
		}
		t.Fatalf("native %s: %v", request.Request.Operation, err)
	}
	var result wire.Result
	if err := json.Unmarshal(bytes.TrimSuffix(stdout, []byte("\n")), &result); err != nil {
		t.Fatalf("decode %s: %v", request.Request.Operation, err)
	}
	return result
}

func envelope(operation wire.Operation, repository, state string, index *string, identities []string) wire.Envelope {
	if identities == nil {
		identities = []string{}
	}
	query := (*string)(nil)
	filters := wire.Filters{PathPrefixes: []string{}, Languages: []string{}, SymbolKinds: []string{}, SourceTypes: []string{}}
	if operation == wire.SearchSymbols || operation == wire.SearchDocs {
		value := "NativeContract"
		query = &value
	}
	if operation == wire.RepositoryMap || operation == wire.SourceSnippets {
		filters = wire.Filters{PathPrefixes: []string{}, Languages: []string{}, SymbolKinds: []string{}, SourceTypes: []string{}}
	}
	if operation == wire.Estimate || operation == wire.Build {
		index = nil
	}
	return wire.Envelope{Phase: phaseForOperation(operation), RepositoryRoot: repository, StateRoot: state, Request: wire.Request{
		SchemaVersion: "1", RequestIdentity: "native-conformance-001", ConsumerIdentity: "taf.work-recovery", Operation: operation,
		RepositoryIdentity: integrationSHA, WorktreeIdentity: integrationSHA, CommittedHead: "0123456789abcdef0123456789abcdef01234567", DirtyOverlayFingerprint: integrationSHA,
		ProviderIdentity: "taf-context", IndexIdentity: index, RequiredCapability: string(operation), MinimumFreshness: "exact",
		Query: query, ResultIdentities: identities, Filters: filters, MaximumResults: 8, MaximumModelOutputCharacters: 4000,
	}}
}

func phaseForOperation(operation wire.Operation) string {
	switch operation {
	case wire.Build:
		return "build"
	case wire.Estimate:
		return "estimate"
	case wire.StatusOperation:
		return "inspect"
	case wire.Metrics:
		return "metrics"
	case wire.Update:
		return "update"
	default:
		return "query"
	}
}
