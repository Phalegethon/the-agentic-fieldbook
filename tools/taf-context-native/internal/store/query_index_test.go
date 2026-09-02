package store

import (
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

func TestMapKindTierPrefersDefinitionsOverImports(t *testing.T) {
	order := []model.RecordKind{model.Module, model.Definition, model.EntryPoint, model.Heading, model.Configuration, model.DocumentChunk, model.Import}
	previous := -1
	for _, kind := range order {
		tier := MapKindTier(kind)
		if tier < previous {
			t.Fatalf("tier order broken at %s: %d < %d", kind, tier, previous)
		}
		previous = tier
	}
	if MapKindTier(model.Definition) != MapKindTier(model.EntryPoint) || MapKindTier(model.Definition) >= MapKindTier(model.Import) {
		t.Fatalf("definition tier %d, entry-point %d, import %d", MapKindTier(model.Definition), MapKindTier(model.EntryPoint), MapKindTier(model.Import))
	}
}

func TestMapGroupsRepresentDefinitionsNotEarlierImports(t *testing.T) {
	importRecord := model.Record{Identity: "sha256:" + strings.Repeat("1", 64), Path: "src/store/authModalStore.ts", StartLine: 1, EndLine: 1, Language: "typescript", RecordKind: model.Import, SourceType: "source", QualifiedName: "create", EvidenceClass: model.Verified}
	definition := model.Record{Identity: "sha256:" + strings.Repeat("2", 64), Path: "src/store/authModalStore.ts", StartLine: 38, EndLine: 57, Language: "typescript", RecordKind: model.Definition, SourceType: "source", QualifiedName: "authModalStore.useAuthModalStore", EvidenceClass: model.Verified}
	index := BuildQueryIndex([]model.Record{importRecord, definition})
	groups, partial := index.MapGroups()
	if partial || len(groups) != 1 || len(groups[0].Ordinals) != 1 || groups[0].Ordinals[0] != 1 {
		t.Fatalf("map groups = %#v partial=%v, want the definition (ordinal 1) as representative", groups, partial)
	}
}

func TestRawKindTierMirrorsMapKindTier(t *testing.T) {
	kinds := []model.RecordKind{model.Module, model.Definition, model.Import, model.EntryPoint, model.Configuration, model.Heading, model.DocumentChunk, model.RecordKind("unknown-kind")}
	for _, kind := range kinds {
		if got, want := rawKindTier([]byte(kind)), MapKindTier(kind); got != want {
			t.Fatalf("rawKindTier(%q) = %d, want MapKindTier = %d", kind, got, want)
		}
	}
}

func TestQueryShortNameIsTheLastDottedSegment(t *testing.T) {
	cases := map[string]string{
		"git_snapshot.collect_snapshot":     "collect_snapshot",
		"query.Search":                      "search",
		"The Agentic Fieldbook.Install TAF": "install taf",
		"Changelog.[Unreleased]#chunk-1":    "[unreleased]#chunk-1",
		"HTTPServer.parse_value-name":       "parse_value-name",
		"---":                               "---",
		"trailing.":                         "",
		"":                                  "",
	}
	for input, want := range cases {
		if got := QueryShortName(input); got != want {
			t.Fatalf("QueryShortName(%q) = %q, want %q", input, got, want)
		}
	}
}
