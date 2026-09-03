package store

import (
	"bytes"
	"cmp"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

func TestMapKindTierPrefersDefinitionsOverImports(t *testing.T) {
	order := []model.RecordKind{model.Module, model.Definition, model.EntryPoint, model.Heading, model.Configuration, model.DocumentChunk, model.Import, model.Reference}
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
	kinds := []model.RecordKind{model.Module, model.Definition, model.Import, model.EntryPoint, model.Configuration, model.Heading, model.DocumentChunk, model.Reference, model.RecordKind("unknown-kind")}
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

// TestCanonicalPathOrderIsUnchangedByPrecomputedKeys freezes the canonical
// path order against the pre-optimisation comparator, which normalized both
// operands on every comparison. byPath, the map groups, the token terms, and
// the encoded payload must stay identical when the comparator changes.
func TestCanonicalPathOrderIsUnchangedByPrecomputedKeys(t *testing.T) {
	records := mixedCaseRecords(2000)
	got := BuildQueryIndex(records)
	want := referenceQueryIndex(records)
	if !slices.Equal(got.PathOrdinals(), want.PathOrdinals()) {
		t.Fatalf("byPath differs from the frozen reference order")
	}
	if !slices.Equal(got.TokenTerms(), want.TokenTerms()) {
		t.Fatalf("token terms differ from the frozen reference")
	}
	gotGroups, gotPartial := got.MapGroups()
	wantGroups, wantPartial := want.MapGroups()
	if gotPartial != wantPartial || !reflect.DeepEqual(gotGroups, wantGroups) {
		t.Fatalf("map groups differ from the frozen reference (partial got=%v want=%v)", gotPartial, wantPartial)
	}
	if !reflect.DeepEqual(got.postings, want.postings) {
		t.Fatalf("query postings differ from the frozen reference")
	}
	encodedGot, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	fixture := filepath.Join("testdata", "mixed-case-2000.bin")
	if os.Getenv("TAF_UPDATE_FIXTURE") == "1" {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(fixture, encodedGot, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Fatalf("wrote %s from the current encoder; unset TAF_UPDATE_FIXTURE and rerun", fixture)
	}
	encodedWant, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(encodedGot, encodedWant) {
		t.Fatalf("encoded payload changed: got %d bytes, want %d bytes", len(encodedGot), len(encodedWant))
	}
}

// TestCanonicalPathOrderBreaksTiesOnNormalizedNameLikeTheReference targets the
// fifth comparator key on its own. mixedCaseRecords never lets a comparison
// reach the normalized-name key while the normalized names actually differ:
// every pair in that fixture is decided earlier (exact path, start line, or
// kind) or arrives at the name key already normalized-equal, so the identity
// tie-break decides instead. nameTieRecords isolates the key by holding path,
// start line, and kind fixed within each family and varying only the
// qualified name, so some pairs are normalized-equal (identity decides, as
// before) and others have genuinely different normalized names (this key must
// decide).
func TestCanonicalPathOrderBreaksTiesOnNormalizedNameLikeTheReference(t *testing.T) {
	records := nameTieRecords(400)
	got := BuildQueryIndex(records)
	want := referenceQueryIndex(records)
	if !slices.Equal(got.PathOrdinals(), want.PathOrdinals()) {
		t.Fatalf("byPath differs from the frozen reference order")
	}
	gotGroups, gotPartial := got.MapGroups()
	wantGroups, wantPartial := want.MapGroups()
	if gotPartial != wantPartial || !reflect.DeepEqual(gotGroups, wantGroups) {
		t.Fatalf("map groups differ from the frozen reference (partial got=%v want=%v)", gotPartial, wantPartial)
	}
}

// nameTieRecords holds path, start line, and record kind fixed within each
// four-record family so the canonical comparator can only be decided by the
// normalized-name key or, when that ties, the identity key. Slots 0 and 1
// share a normalized qualified name ("alpha") but differ in case and
// surrounding whitespace, so NormalizeQueryText must equalize them before the
// identity tie-break can run. Slots 2 and 3 mirror that shape around "beta",
// and "alpha" sorts before "beta" only after normalization strips the case
// and whitespace noise, so a comparator that ignored this key entirely would
// also happen to reorder slot 0/1 against slot 2/3.
func nameTieRecords(count int) []model.Record {
	records := make([]model.Record, 0, count)
	for index := 0; len(records) < count; index++ {
		family := index / 4
		slot := index % 4
		path := fmt.Sprintf("src/tie/Module%03d.go", family)
		var qualifiedName string
		switch slot {
		case 0:
			qualifiedName = fmt.Sprintf("  MODULE%03d.ALPHA  ", family)
		case 1:
			qualifiedName = fmt.Sprintf("module%03d.alpha", family)
		case 2:
			qualifiedName = fmt.Sprintf("  MODULE%03d.BETA  ", family)
		case 3:
			qualifiedName = fmt.Sprintf("module%03d.beta", family)
		}
		records = append(records, model.Record{
			Identity:         fmt.Sprintf("sha256:%064x", index),
			Path:             path,
			StartLine:        1,
			EndLine:          9,
			Language:         "go",
			RecordKind:       model.Definition,
			SourceType:       "source",
			QualifiedName:    qualifiedName,
			ExtractionMethod: "go/parser@go1.27",
			EvidenceClass:    model.Verified,
			SearchTerms:      []string{"tie", fmt.Sprintf("module%03d", family)},
			SourceDigest:     "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		})
	}
	return records
}

// mixedCaseRecords is deterministic and exercises every level of the canonical
// path comparator: differing normalized paths, equal normalized paths that
// differ in case, equal paths differing in start line and in record kind, and
// equal normalized qualified names that differ only in surrounding space and
// case so the identity tie-break decides.
func mixedCaseRecords(count int) []model.Record {
	records := make([]model.Record, 0, count)
	for index := 0; len(records) < count; index++ {
		family := index / 8
		slot := index % 8
		path := fmt.Sprintf("src/Module%03d/Handler.go", family)
		if slot >= 4 {
			path = strings.ToLower(path)
		}
		record := model.Record{
			Identity:         fmt.Sprintf("sha256:%064x", index),
			Path:             path,
			StartLine:        1,
			EndLine:          9,
			Language:         "go",
			RecordKind:       model.Definition,
			SourceType:       "source",
			QualifiedName:    fmt.Sprintf("Module%03d.Handler", family),
			ExtractionMethod: "go/parser@go1.27",
			EvidenceClass:    model.Verified,
			SearchTerms:      []string{"handler", fmt.Sprintf("module%03d", family)},
			SourceDigest:     "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		}
		switch slot % 4 {
		case 0:
			record.QualifiedName = "  " + record.QualifiedName + "  "
		case 1:
			record.QualifiedName = strings.ToLower(record.QualifiedName)
			record.EvidenceClass = model.Inferred
		case 2:
			record.RecordKind = model.Import
			record.QualifiedName = "Fmt"
			record.SearchTerms = []string{"fmt", "import"}
		case 3:
			record.StartLine = 42
			record.EndLine = 44
			record.QualifiedName = fmt.Sprintf("Module%03d.Other", family)
			record.SearchTerms = []string{"other", fmt.Sprintf("module%03d", family)}
		}
		records = append(records, record)
	}
	return records
}

// referenceQueryIndex reproduces the pre-optimisation ordering: the byPath
// sort and the map grouping as they were before normalized keys were
// precomputed. It intentionally duplicates the production logic so a change
// there cannot silently move the reference too.
func referenceQueryIndex(records []model.Record) QueryIndex {
	index := QueryIndex{postings: make(map[string][]uint32)}
	for ordinal, record := range records {
		visitCanonicalQueryKeys(record, func(key string) bool {
			index.postings[key] = append(index.postings[key], uint32(ordinal))
			return true
		})
	}
	index.tokenTerms = make([]string, 0, len(index.postings))
	for key := range index.postings {
		if strings.HasPrefix(key, queryTokenPrefix) {
			index.tokenTerms = append(index.tokenTerms, strings.TrimPrefix(key, queryTokenPrefix))
		}
	}
	slices.Sort(index.tokenTerms)
	index.byPath = make([]uint32, len(records))
	for ordinal := range records {
		index.byPath[ordinal] = uint32(ordinal)
	}
	slices.SortFunc(index.byPath, func(left, right uint32) int {
		return referenceComparePathOrdinal(records, left, right)
	})
	index.mapGroups, index.mapPartial = referenceMapGroups(records, index.byPath, policy.ProductionLimits().MaximumLexicalCandidates)
	return index
}

func referenceComparePathOrdinal(records []model.Record, left, right uint32) int {
	l, r := records[left], records[right]
	for _, comparison := range []int{
		cmp.Compare(referenceNormalize(l.Path), referenceNormalize(r.Path)),
		cmp.Compare(l.Path, r.Path),
		cmp.Compare(l.StartLine, r.StartLine),
		cmp.Compare(string(l.RecordKind), string(r.RecordKind)),
		cmp.Compare(referenceNormalize(l.QualifiedName), referenceNormalize(r.QualifiedName)),
		cmp.Compare(l.Identity, r.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func referenceMapGroups(records []model.Record, pathOrdinals []uint32, maximum int) ([]QueryMapGroup, bool) {
	if maximum < 1 {
		return nil, len(pathOrdinals) != 0
	}
	groups := make([]QueryMapGroup, 0, min(len(pathOrdinals), maximum))
	partial := false
	for start := 0; start < len(pathOrdinals); {
		end := start + 1
		path := records[pathOrdinals[start]].Path
		best := pathOrdinals[start]
		for end < len(pathOrdinals) && records[pathOrdinals[end]].Path == path {
			if referenceCompareMapRepresentative(records[pathOrdinals[end]], records[best]) < 0 {
				best = pathOrdinals[end]
			}
			end++
		}
		if len(groups) == maximum {
			partial = true
			break
		}
		groups = append(groups, QueryMapGroup{Path: path, Ordinals: []uint32{best}})
		start = end
	}
	return groups, partial
}

func referenceCompareMapRepresentative(left, right model.Record) int {
	for _, comparison := range []int{
		cmp.Compare(referenceEvidenceTier(left), referenceEvidenceTier(right)),
		cmp.Compare(referenceSourceTier(left), referenceSourceTier(right)),
		cmp.Compare(referenceKindTier(left.RecordKind), referenceKindTier(right.RecordKind)),
		cmp.Compare(left.StartLine, right.StartLine),
		cmp.Compare(referenceNormalize(left.QualifiedName), referenceNormalize(right.QualifiedName)),
		cmp.Compare(left.Identity, right.Identity),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func referenceNormalize(value string) string { return strings.ToLower(strings.TrimSpace(value)) }

func referenceEvidenceTier(record model.Record) int {
	switch record.EvidenceClass {
	case model.Verified:
		return 0
	case model.Inferred:
		return 1
	default:
		return 2
	}
}

func referenceSourceTier(record model.Record) int {
	switch record.SourceType {
	case "source":
		return 0
	case "document":
		return 1
	default:
		return 2
	}
}

func referenceKindTier(kind model.RecordKind) int {
	switch kind {
	case model.Module:
		return 0
	case model.Definition, model.EntryPoint:
		return 1
	case model.Heading:
		return 2
	case model.Configuration:
		return 3
	case model.DocumentChunk:
		return 4
	case model.Import:
		return 5
	default:
		return 6
	}
}

// TestReferenceRecordsAreKeyedByTargetNotByEnclosingName freezes the index
// keys of a reference record: it is found by the names it refers to, so the
// qualified key of every target name and the short key of that name's last
// segment carry it, while the enclosing definition's own name keys stay on the
// definition record alone. Two targets sharing a last segment contribute the
// short key once: the encoder rejects a record that claims the same key twice.
func TestReferenceRecordsAreKeyedByTargetNotByEnclosingName(t *testing.T) {
	definition := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"run"})
	reference := testRecord(testRecordB, "pkg/a.py", "a.run", []string{"helpers.load", "osp.load"})
	reference.RecordKind = model.Reference
	reference.TargetName, reference.ReferenceCount = "helpers.load:5:2;osp.load:7:1", 3
	records := []model.Record{definition, reference}
	index := BuildQueryIndex(records)

	for key, want := range map[string][]uint32{"helpers.load": {1}, "osp.load": {1}, "a.run": {0}} {
		if got := index.QualifiedOrdinals(key); !slices.Equal(got, want) {
			t.Fatalf("qualified(%q) = %v, want %v", key, got, want)
		}
	}
	if got := index.ShortOrdinals("load"); !slices.Equal(got, []uint32{1}) {
		t.Fatalf("short(load) = %v, want the reference once", got)
	}
	if got := index.ShortOrdinals("run"); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("short(run) = %v, the reference must not be keyed by its enclosing name", got)
	}
	if got := index.TokenOrdinals("run"); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("token(run) = %v, the reference must not be tokenized by its enclosing name", got)
	}
	if got := index.TokenOrdinals("helpers.load"); len(got) != 0 {
		t.Fatalf("token(helpers.load) = %v, a target name is not sub-tokenized into the token postings", got)
	}
	if got := index.FacetOrdinals(QueryFacetKind, string(model.Reference)); !slices.Equal(got, []uint32{1}) {
		t.Fatalf("kind facet = %v, want the reference record", got)
	}
	if got := index.FacetOrdinals(QueryFacetOperation, queryOperationSymbols); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("symbols facet = %v, want the definition alone", got)
	}
	groups, partial := index.MapGroups()
	for _, group := range groups {
		if slices.Contains(group.Ordinals, uint32(1)) {
			t.Fatalf("map groups %#v (partial=%v) must not contain the reference record", groups, partial)
		}
	}
	keys := canonicalQueryKeys(reference)
	if slices.Compact(slices.Clone(keys)) == nil || len(slices.Compact(slices.Clone(keys))) != len(keys) {
		t.Fatalf("keys %v repeat: the encoder counts one posting per emitted key", keys)
	}
	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := validateIndex(encoded); err != nil {
		t.Fatalf("validateIndex = %v", err)
	}
}
