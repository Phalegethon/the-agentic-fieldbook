package integration

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const (
	dogfoodMinimumHits = 39
	dogfoodEntryCount  = 41
)

type dogfoodFixture struct {
	SchemaVersion string         `json:"schema_version"`
	Description   string         `json:"description"`
	Entries       []dogfoodEntry `json:"entries"`
}

type dogfoodEntry struct {
	ID             string         `json:"id"`
	Operation      wire.Operation `json:"operation"`
	Query          string         `json:"query"`
	Languages      []string       `json:"languages"`
	SymbolKinds    []string       `json:"symbol_kinds"`
	ExpectedPath   string         `json:"expected_path"`
	ExpectedSymbol string         `json:"expected_symbol"`
}

// TestDogfoodRecallOnThisRepository indexes the checkout that contains this
// test and asks for real symbols and headings the way an agent would. It is
// the user-facing recall gate for the Level 1 engine.
func TestDogfoodRecallOnThisRepository(t *testing.T) {
	repository, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, statErr := os.Lstat(filepath.Join(repository, ".git")); statErr != nil {
		t.Skipf("no repository checkout above the module: %v", statErr)
	}
	raw, err := os.ReadFile(filepath.Join("..", "testdata", "dogfood", "recall.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture dogfoodFixture
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.SchemaVersion != "1" || len(fixture.Entries) != dogfoodEntryCount {
		t.Fatalf("fixture = version %q with %d entries, want 1 and %d", fixture.SchemaVersion, len(fixture.Entries), dogfoodEntryCount)
	}

	binary := buildNativeBinary(t)
	state := filepath.Join(t.TempDir(), "state")
	built := invoke(t, binary, envelope(wire.Build, repository, state, nil, nil))
	if built.IndexIdentity == nil || (built.Status != wire.Ready && built.Status != wire.Partial) {
		t.Fatalf("build = %#v", built)
	}

	hits := 0
	var misses []string
	for _, entry := range fixture.Entries {
		request := envelope(entry.Operation, repository, state, built.IndexIdentity, nil)
		query := entry.Query
		request.Request.Query = &query
		request.Request.MaximumResults = 8
		// The engine-side gate measures ranking, not output trimming; the
		// broker dogfood (Python) runs with the default 4000.
		request.Request.MaximumModelOutputCharacters = 12000
		request.Request.Filters = wire.Filters{PathPrefixes: []string{}, Languages: orEmpty(entry.Languages), SymbolKinds: orEmpty(entry.SymbolKinds), SourceTypes: []string{}}
		result := invoke(t, binary, request)
		if result.Status != wire.Ready {
			t.Errorf("%s: status %s (warnings %v), want ready", entry.ID, result.Status, result.Warnings)
		}
		if !result.Truncated && containsWarning(result.Warnings, "query-frontier-exhausted") {
			t.Errorf("%s: exhausted search without truncated flag: %#v", entry.ID, result)
		}
		rank := 0
		for _, finding := range result.Findings {
			if finding.Path != entry.ExpectedPath || !strings.EqualFold(lastSegment(finding.QualifiedName), entry.ExpectedSymbol) {
				continue
			}
			rank = finding.Rank
			if (finding.RecordKind == "definition" || finding.RecordKind == "heading") && finding.Preview == "" {
				t.Errorf("%s: empty preview on %s", entry.ID, finding.QualifiedName)
			}
			break
		}
		if rank == 0 {
			misses = append(misses, entry.ID)
			continue
		}
		hits++
	}
	t.Logf("recall@8 = %d/%d, misses = %v", hits, len(fixture.Entries), misses)
	if hits < dogfoodMinimumHits {
		t.Fatalf("recall@8 = %d/%d below %d; misses: %v", hits, len(fixture.Entries), dogfoodMinimumHits, misses)
	}
	assertStorageRatio(t, repository, state)
}

func orEmpty(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func lastSegment(qualified string) string {
	if index := strings.LastIndexByte(qualified, '.'); index >= 0 {
		return qualified[index+1:]
	}
	return qualified
}

func containsWarning(warnings []string, wanted string) bool {
	for _, warning := range warnings {
		if warning == wanted {
			return true
		}
	}
	return false
}

// assertStorageRatio approximates the indexed source size by the files the
// engine's language set covers, skipping vendored and metadata directories,
// and checks the state root against the frozen storage ratio ceiling.
func assertStorageRatio(t *testing.T, repository, state string) {
	t.Helper()
	extensions := map[string]bool{".py": true, ".js": true, ".ts": true, ".tsx": true, ".go": true, ".rs": true, ".md": true, ".json": true, ".toml": true}
	skipped := map[string]bool{".git": true, ".worktrees": true, "vendor": true, "node_modules": true}
	var sourceBytes int64
	walkErr := filepath.WalkDir(repository, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if skipped[entry.Name()] && path != repository {
				return filepath.SkipDir
			}
			return nil
		}
		if !extensions[filepath.Ext(entry.Name())] {
			return nil
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			return infoErr
		}
		sourceBytes += info.Size()
		return nil
	})
	if walkErr != nil {
		t.Fatal(walkErr)
	}
	var stateBytes int64
	if err := filepath.WalkDir(state, func(path string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			return infoErr
		}
		stateBytes += info.Size()
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	ratio := float64(stateBytes) / float64(max(sourceBytes, 1))
	t.Logf("storage ratio = %.3f (state %d bytes, source %d bytes)", ratio, stateBytes, sourceBytes)
	if ratio > policy.ProductionLimits().StorageToSourceRatioMaximum {
		t.Fatalf("storage ratio %.3f exceeds %.2f", ratio, policy.ProductionLimits().StorageToSourceRatioMaximum)
	}
}
