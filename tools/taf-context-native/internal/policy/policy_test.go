package policy

import (
	"encoding/json"
	"fmt"
	"io"
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
	artifact, err := parseExactPolicyJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	expected := Limits{SchemaVersion: "1", MaximumWireBytes: 262144, MaximumStdoutBytes: 262144, MaximumStderrBytes: 65536, MaximumCollectionItems: 64, MaximumEligiblePaths: 250000, MaximumChangedPaths: 10000, MaximumEligibleSourceBytes: 4294967296, MaximumSourceFileBytes: 2097152, MaximumMarkdownFileBytes: 8388608, MaximumLexicalCandidates: 4096, MaximumTermsPerWord: 4096, MaximumDictionaryTerms: 262144, MaximumFuzzyDistance: 2, BuildLatencyNSMaximum: 10000000000, QueryLatencyP95NSMaximum: 150000000, UpdateLatencyNSMaximum: 2000000000, PeakMemoryBytesMaximum: 536870912, StorageToSourceRatioMaximum: 1.5}
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
	if err := assertFrozenModule(string(raw)); err != nil {
		t.Fatal(err)
	}
}

func TestPolicyAndModuleTestsRejectDrift(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "policy", "production-v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var artifact map[string]json.RawMessage
	if err := json.Unmarshal(raw, &artifact); err != nil {
		t.Fatal(err)
	}
	artifact["unexpected"] = json.RawMessage("0")
	mutated, _ := json.Marshal(artifact)
	if _, err := parseExactPolicyJSON(mutated); err == nil {
		t.Fatal("accepted an extra policy key")
	}
	for _, mutatedModule := range []string{
		"module github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native\ngo 1.27.1\n",
		"module github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/v2\ngo 1.27.0\n",
		"module github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native\ngo 1.27.0\nreplace example.com/x => example.com/y\n",
	} {
		if err := assertFrozenModule(mutatedModule); err == nil {
			t.Fatalf("accepted module drift: %q", mutatedModule)
		}
	}
}

func parseExactPolicyJSON(raw []byte) (Limits, error) {
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	var limits Limits
	if err := decoder.Decode(&limits); err != nil {
		return Limits{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return Limits{}, fmt.Errorf("unexpected trailing policy JSON: %w", err)
	}
	return limits, nil
}

func assertFrozenModule(raw string) error {
	expectedDirect := map[string]string{
		"github.com/tree-sitter/go-tree-sitter":         "v0.25.0",
		"github.com/tree-sitter/tree-sitter-javascript": "v0.25.0",
		"github.com/tree-sitter/tree-sitter-python":     "v0.25.0",
		"github.com/tree-sitter/tree-sitter-rust":       "v0.24.2",
		"github.com/tree-sitter/tree-sitter-typescript": "v0.23.2",
	}
	expectedIndirect := map[string]string{"github.com/mattn/go-pointer": "v0.0.1"}
	direct, indirect := map[string]string{}, map[string]string{}
	module, goVersion := "", ""
	inRequireBlock := false
	for _, line := range strings.Split(raw, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || strings.HasPrefix(fields[0], "//") {
			continue
		}
		if fields[0] == "replace" || fields[0] == "exclude" || fields[0] == "tool" {
			return fmt.Errorf("forbidden module directive %q", fields[0])
		}
		if fields[0] == ")" {
			if !inRequireBlock || len(fields) != 1 {
				return fmt.Errorf("invalid require block terminator")
			}
			inRequireBlock = false
			continue
		}
		if fields[0] == "module" {
			if len(fields) != 2 || module != "" {
				return fmt.Errorf("invalid module directive")
			}
			module = fields[1]
			continue
		}
		if fields[0] == "go" {
			if len(fields) != 2 || goVersion != "" {
				return fmt.Errorf("invalid go directive")
			}
			goVersion = fields[1]
			continue
		}
		if fields[0] == "require" {
			fields = fields[1:]
			if len(fields) == 1 && fields[0] == "(" {
				if inRequireBlock {
					return fmt.Errorf("nested require block")
				}
				inRequireBlock = true
				continue
			}
			if inRequireBlock {
				return fmt.Errorf("nested require declaration")
			}
		} else if !inRequireBlock {
			return fmt.Errorf("unexpected module line %q", line)
		}
		if len(fields) < 2 || len(fields) > 4 || (len(fields) > 2 && (len(fields) != 4 || fields[2] != "//" || fields[3] != "indirect")) {
			return fmt.Errorf("invalid require declaration %q", line)
		}
		if len(fields) == 4 {
			indirect[fields[0]] = fields[1]
		} else {
			direct[fields[0]] = fields[1]
		}
	}
	if inRequireBlock || module != "github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native" || goVersion != "1.27.0" || !reflect.DeepEqual(direct, expectedDirect) || !reflect.DeepEqual(indirect, expectedIndirect) {
		return fmt.Errorf("module contract drift: module=%q go=%q direct=%v indirect=%v", module, goVersion, direct, indirect)
	}
	return nil
}
