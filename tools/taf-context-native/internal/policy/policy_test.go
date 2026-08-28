package policy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestLimitsMatchFrozenProductionPolicy(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "policy", "production-v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var artifact Limits
	if err := json.Unmarshal(raw, &artifact); err != nil {
		t.Fatal(err)
	}
	expected := Limits{SchemaVersion: "1", MaximumWireBytes: 262144, MaximumStdoutBytes: 262144, MaximumStderrBytes: 65536, MaximumCollectionItems: 64, MaximumEligiblePaths: 250000, MaximumChangedPaths: 10000, MaximumEligibleSourceBytes: 4294967296, MaximumSourceFileBytes: 2097152, MaximumMarkdownFileBytes: 8388608, MaximumLexicalCandidates: 4096, MaximumFuzzyTerms: 64, MaximumFuzzyDistance: 2, BuildLatencyNSMaximum: 10000000000, QueryLatencyP95NSMaximum: 150000000, UpdateLatencyNSMaximum: 2000000000, PeakMemoryBytesMaximum: 536870912, StorageToSourceRatioMaximum: 1.5}
	if !reflect.DeepEqual(artifact, expected) {
		t.Fatalf("policy artifact differs from frozen policy: artifact=%+v expected=%+v", artifact, expected)
	}
	if !reflect.DeepEqual(ProductionLimits(), expected) {
		t.Fatalf("runtime policy differs from frozen policy: runtime=%+v expected=%+v", ProductionLimits(), expected)
	}
}

func TestModulePinsAreFrozen(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	for _, required := range []string{
		"module github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native",
		"github.com/tree-sitter/go-tree-sitter v0.24.0",
		"github.com/tree-sitter/tree-sitter-javascript v0.25.0",
		"github.com/tree-sitter/tree-sitter-python v0.25.0",
		"github.com/tree-sitter/tree-sitter-rust v0.24.2",
		"github.com/tree-sitter/tree-sitter-typescript v0.23.2",
	} {
		if !strings.Contains(string(raw), required) {
			t.Fatalf("missing frozen module pin %q", required)
		}
	}
}
