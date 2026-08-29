package store

import (
	"bytes"
	"compress/zlib"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"runtime/debug"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
	"unsafe"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

func TestBuildContextCancelledBeforePreparationCreatesNoState(t *testing.T) {
	root, state := storeRoots(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := BuildContext(ctx, root, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}); !errors.Is(err, context.Canceled) {
		t.Fatalf("BuildContext error = %v", err)
	}
	if _, err := os.Stat(state); !os.IsNotExist(err) {
		t.Fatalf("canceled build created state: %v", err)
	}
}

func TestBuildContextSameGenerationCancellationDuringMaterializationReturnsError(t *testing.T) {
	roots, _ := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	ctx, cancel := context.WithCancel(context.Background())
	_, err := buildWithFilesystemObservedContext(ctx, boundaryFilesystem{}, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}, buildHooks{materialized: cancel})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v", err)
	}
	loaded, loadErr := Load(roots, first.IndexIdentity)
	if loadErr != nil {
		t.Fatal(loadErr)
	}
	if loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("CURRENT changed: %s", loaded.IndexIdentity)
	}
}

func TestBuildContextCancellationDuringPreparationStagesPreservesCurrent(t *testing.T) {
	stages := []buildPhase{
		buildPhasePreflight,
		buildPhaseQueryKeys,
		buildPhaseSort,
		buildPhaseEncode,
		buildPhaseCompression,
		buildPhasePayloadDigest,
		buildPhaseManifest,
		buildPhaseGenerationDigest,
	}
	for index, stage := range stages {
		t.Run(string(stage), func(t *testing.T) {
			roots, state := storeRoots(t)
			prior := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
			priorCurrent := mustRead(t, filepath.Join(state, currentFilename))
			records := buildCancellationRecords(8192)
			ctx, cancel := context.WithCancel(context.Background())
			calls := 0
			hooks := buildHooks{building: func(current buildPhase) {
				if current == stage {
					calls++
					if calls == 2 {
						cancel()
					}
				}
			}}
			snapshot, err := buildWithFilesystemObservedContext(ctx, boundaryFilesystem{}, roots, manifestVariant(fmt.Sprintf("%x", index)), records, hooks)
			if err != context.Canceled {
				t.Fatalf("BuildContext error = %v, want context.Canceled", err)
			}
			if !reflect.DeepEqual(snapshot, Snapshot{}) {
				t.Fatalf("canceled build exposed snapshot: %#v", snapshot)
			}
			if calls < 2 {
				t.Fatalf("stage %q checkpoints = %d, want at least 2", stage, calls)
			}
			if current := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(current, priorCurrent) {
				t.Fatalf("stage %q changed CURRENT: %q, want %q", stage, current, priorCurrent)
			}
			loaded, loadErr := Load(roots, prior.IndexIdentity)
			if loadErr != nil || loaded.IndexIdentity != prior.IndexIdentity {
				t.Fatalf("stage %q prior CURRENT load = %#v, %v", stage, loaded, loadErr)
			}
		})
	}
}

func TestBuildContextCancellationDuringLongRecordValidationWinsOverValidity(t *testing.T) {
	tests := []struct {
		name    string
		preview string
		want    error
	}{
		{name: "valid", preview: strings.Repeat("v", maximumIndexStringBytes), want: context.Canceled},
		{name: "invalid", preview: strings.Repeat("v", maximumIndexStringBytes-1) + "\x00", want: context.DeadlineExceeded},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			roots, state := storeRoots(t)
			record := testRecord(testRecordA, "a.go", "A", []string{"a"})
			record.Preview = test.preview
			canceled := false
			ctx := stagedBuildContext{Context: context.Background(), canceled: &canceled, err: test.want}
			sawQueryKeys := false
			observations := 0
			hooks := buildHooks{building: func(phase buildPhase) {
				if phase == buildPhaseQueryKeys {
					sawQueryKeys = true
				}
				if sawQueryKeys && phase == buildPhaseValidation {
					observations++
					if observations == 2 {
						canceled = true
					}
				}
			}}
			snapshot, err := buildWithFilesystemObservedContext(ctx, boundaryFilesystem{}, roots, testManifest(), []model.Record{record}, hooks)
			if err != test.want {
				t.Fatalf("BuildContext error = %v, want %v", err, test.want)
			}
			if !reflect.DeepEqual(snapshot, Snapshot{}) || observations != 2 {
				t.Fatalf("snapshot/observations = %#v/%d", snapshot, observations)
			}
			if _, err := os.Stat(state); !os.IsNotExist(err) {
				t.Fatalf("canceled validation created state: %v", err)
			}
		})
	}

	t.Run("invalid without cancellation", func(t *testing.T) {
		roots, state := storeRoots(t)
		record := testRecord(testRecordA, "a.go", "A", []string{"a"})
		record.Preview = strings.Repeat("v", maximumIndexStringBytes-1) + "\x00"
		if _, err := BuildContext(context.Background(), roots, testManifest(), []model.Record{record}); !errors.Is(err, ErrInvalidIndex) {
			t.Fatalf("BuildContext error = %v, want ErrInvalidIndex", err)
		}
		if _, err := os.Stat(state); !os.IsNotExist(err) {
			t.Fatalf("invalid build created state: %v", err)
		}
	})
}

func TestPostingRangeCountContextCancelsAcrossIsolatedRanges(t *testing.T) {
	ordinals := make([]uint32, 8192)
	for index := range ordinals {
		ordinals[index] = uint32(index * 2)
	}
	canceled := false
	ctx := stagedBuildContext{Context: context.Background(), canceled: &canceled, err: context.Canceled}
	observations := 0
	count, err := postingRangeCountContext(ctx, ordinals, func(phase buildPhase) {
		if phase == buildPhaseRangeCount {
			observations++
			if observations == 4 {
				canceled = true
			}
		}
	})
	if err != context.Canceled {
		t.Fatalf("postingRangeCountContext error = %v, want context.Canceled", err)
	}
	if count != 0 || observations != 4 {
		t.Fatalf("count/observations = %d/%d, want 0/4", count, observations)
	}
}

func TestBuildContextCancellationDuringConsecutiveQueryRangeSerializationPreservesState(t *testing.T) {
	tests := []struct {
		name         string
		prior        bool
		contextError error
	}{
		{name: "initial", contextError: context.Canceled},
		{name: "replacement", prior: true, contextError: context.DeadlineExceeded},
	}
	records := buildCancellationRecords(8192)
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			roots, state := storeRoots(t)
			var prior Snapshot
			var priorCurrent []byte
			if test.prior {
				prior = mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
				priorCurrent = mustRead(t, filepath.Join(state, currentFilename))
			}

			canceled := false
			ctx := stagedBuildContext{Context: context.Background(), canceled: &canceled, err: test.contextError}
			sawRangeCount := false
			rangeEncodeBeforeCount := false
			rangeEncodeObservations := 0
			hooks := buildHooks{building: func(phase buildPhase) {
				if phase == buildPhaseRangeCount {
					sawRangeCount = true
				}
				if phase == buildPhaseRangeEncode {
					if !sawRangeCount {
						rangeEncodeBeforeCount = true
					}
					rangeEncodeObservations++
					if rangeEncodeObservations == 2 {
						canceled = true
					}
				}
			}}

			snapshot, err := buildWithFilesystemObservedContext(ctx, boundaryFilesystem{}, roots, testManifest(), records, hooks)
			if err != test.contextError {
				t.Fatalf("BuildContext error = %v, want exact %v", err, test.contextError)
			}
			if !reflect.DeepEqual(snapshot, Snapshot{}) {
				t.Fatalf("canceled build exposed snapshot: %#v", snapshot)
			}
			if !sawRangeCount || rangeEncodeBeforeCount || rangeEncodeObservations != 2 {
				t.Fatalf("range count/encode-before-count/encode observations = %t/%t/%d, want true/false/2", sawRangeCount, rangeEncodeBeforeCount, rangeEncodeObservations)
			}

			if !test.prior {
				if _, err := os.Stat(state); !os.IsNotExist(err) {
					t.Fatalf("canceled initial serialization created state: %v", err)
				}
				return
			}
			if current := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(current, priorCurrent) {
				t.Fatalf("canceled replacement changed CURRENT: %q, want %q", current, priorCurrent)
			}
			loaded, loadErr := Load(roots, prior.IndexIdentity)
			if loadErr != nil || loaded.IndexIdentity != prior.IndexIdentity {
				t.Fatalf("prior CURRENT load = %#v, %v", loaded, loadErr)
			}
		})
	}
}

type stagedBuildContext struct {
	context.Context
	canceled *bool
	err      error
}

func (ctx stagedBuildContext) Err() error {
	if ctx.canceled != nil && *ctx.canceled {
		return ctx.err
	}
	return ctx.Context.Err()
}

func buildCancellationRecords(count int) []model.Record {
	records := make([]model.Record, count)
	for index := range records {
		records[index] = testRecord(
			fmt.Sprintf("sha256:%064x", count-index),
			fmt.Sprintf("pkg/%05d/service.go", index),
			fmt.Sprintf("pkg.Service%05d", index),
			[]string{"service", fmt.Sprintf("service%05d", index)},
		)
	}
	return records
}

const (
	testRecordA = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testRecordB = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func TestEncodeIndexIsDeterministicAndDoesNotMutateInputs(t *testing.T) {
	first := []model.Record{
		testRecord(testRecordB, "z/service.go", "z.Service", []string{"service", "run"}),
		testRecord(testRecordA, "a/service.go", "a.Service", []string{"service", "api"}),
	}
	second := []model.Record{cloneRecord(first[1]), cloneRecord(first[0])}
	second[0].SearchTerms = []string{"api", "service"}
	second[1].SearchTerms = []string{"run", "service"}
	wantFirst := cloneRecords(first)
	wantSecond := cloneRecords(second)

	left, err := encodeIndex(first)
	if err != nil {
		t.Fatal(err)
	}
	right, err := encodeIndex(second)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(left, right) {
		t.Fatal("record/input permutation changed index bytes")
	}
	if !recordsEqual(first, wantFirst) || !recordsEqual(second, wantSecond) {
		t.Fatal("encodeIndex mutated caller-owned records")
	}

	records, postings, err := decodeIndex(left)
	if err != nil {
		t.Fatal(err)
	}
	if got := []string{records[0].Identity, records[1].Identity}; !slices.Equal(got, []string{testRecordA, testRecordB}) {
		t.Fatalf("record identities = %v", got)
	}
	if !slices.Equal(postings["service"], []uint32{0, 1}) || !slices.Equal(postings["api"], []uint32{0}) || !slices.Equal(postings["run"], []uint32{1}) {
		t.Fatalf("postings = %#v", postings)
	}

	repeated, err := encodeIndex(first)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(left, repeated) {
		t.Fatal("repeated encoding changed bytes")
	}
}

func TestIndexV2PersistsCanonicalQueryStructures(t *testing.T) {
	records := []model.Record{
		testRecord(testRecordB, "docs/guide.md", "Guide Setup", []string{"guide", "setup"}),
		testRecord(testRecordA, "pkg/service.go", "pkg.Service", []string{"alias", "service"}),
	}
	records[0].RecordKind, records[0].SourceType, records[0].Language = model.Heading, "document", "markdown"

	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	plain := mustDecompress(t, encoded)
	if version := binary.BigEndian.Uint16(plain[len(indexMagic):]); version != 2 {
		t.Fatalf("index version = %d, want 2", version)
	}
	decoded, postings, queryIndex, err := decodeIndexContextWithQueryObserved(context.Background(), encoded, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(decoded) != 2 || !slices.Equal(postings["service"], []uint32{0}) {
		t.Fatalf("decoded records/postings = %#v %#v", decoded, postings)
	}
	if got := queryIndex.QualifiedOrdinals("pkg.Service"); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("qualified postings = %v", got)
	}
	if got := queryIndex.ShortOrdinals("Service"); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("short postings = %v", got)
	}
	if got := queryIndex.TokenOrdinals("setup"); !slices.Equal(got, []uint32{1}) {
		t.Fatalf("token postings = %v", got)
	}
	if got := queryIndex.FacetOrdinals(QueryFacetLanguage, "markdown"); !slices.Equal(got, []uint32{1}) {
		t.Fatalf("language facet = %v", got)
	}
	if got := queryIndex.PathOrdinals(); !slices.Equal(got, []uint32{1, 0}) {
		t.Fatalf("path ordinals = %v", got)
	}
	groups, partial := queryIndex.MapGroups()
	if partial || len(groups) != 2 || groups[0].Path != "docs/guide.md" || groups[1].Path != "pkg/service.go" {
		t.Fatalf("map groups = %#v partial=%v", groups, partial)
	}
}

func TestIndexV2OmitsEmptyDerivedShortAliasButKeepsQualifiedPosting(t *testing.T) {
	record := testRecord(testRecordA, "punctuation.go", "---", []string{"punctuation"})
	encoded, err := encodeIndex([]model.Record{record})
	if err != nil {
		t.Fatal(err)
	}
	_, _, queryIndex, err := decodeIndexContext(context.Background(), encoded)
	if err != nil {
		t.Fatal(err)
	}
	if got := queryIndex.QualifiedOrdinals("---"); !slices.Equal(got, []uint32{0}) {
		t.Fatalf("qualified punctuation posting = %v", got)
	}
	if got := queryIndex.ShortOrdinals(""); len(got) != 0 {
		t.Fatalf("empty short alias posting = %v", got)
	}
}

func TestIndexV2RejectsOldShapeReservedTermsAndQuerySectionCorruption(t *testing.T) {
	record := testRecord(testRecordA, "pkg/service.go", "pkg.Service", []string{"service"})
	reserved := record
	reserved.SearchTerms = []string{"~taf-query/t/service"}
	if _, err := encodeIndex([]model.Record{reserved}); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("reserved term error = %v, want ErrInvalidIndex", err)
	}

	encoded, err := encodeIndex([]model.Record{record})
	if err != nil {
		t.Fatal(err)
	}
	old := mutateHeader(encoded, func(header []byte) { binary.BigEndian.PutUint16(header[len(indexMagic):], 1) })
	if _, _, _, err := decodeIndexContextWithQueryObserved(context.Background(), old, nil); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("old shape decode error = %v, want ErrInvalidIndex", err)
	}
	if _, _, err := validateIndex(old); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("old shape validation error = %v, want ErrInvalidIndex", err)
	}

	corruptPayload := mutateFirstQueryOrdinal(t, encoded, 99)
	if _, _, _, err := decodeIndexContextWithQueryObserved(context.Background(), corruptPayload, nil); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("corrupt query decode error = %v, want ErrInvalidIndex", err)
	}
	if _, _, err := validateIndex(corruptPayload); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("corrupt query validation error = %v, want ErrInvalidIndex", err)
	}
}

func TestLoadContextCancelsDuringLargeMaterializationWithoutSnapshot(t *testing.T) {
	records := make([]model.Record, 150_000)
	for index := range records {
		records[index] = testRecord(fmt.Sprintf("sha256:%064x", index), fmt.Sprintf("pkg/%06d.go", index), fmt.Sprintf("Service%06d", index), []string{"service"})
	}
	roots, _ := storeRoots(t)
	identity := mustBuild(t, roots, testManifest(), records).IndexIdentity
	ctx, cancel := context.WithCancel(context.Background())
	checkpoints := 0
	snapshot, err := loadContextObserved(ctx, roots, identity, func() {
		checkpoints++
		if checkpoints == 150 {
			cancel()
		}
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("LoadContext error = %v, want context.Canceled", err)
	}
	if snapshot.Records != nil || snapshot.Postings != nil || !snapshot.Query.Empty() || snapshot.IndexIdentity != "" {
		t.Fatalf("canceled load exposed snapshot state: %#v", snapshot)
	}
	if checkpoints != 150 {
		t.Fatalf("checkpoints = %d, want prompt cancellation during auxiliary construction at 150", checkpoints)
	}
}

func TestEncodeIndexRejectsDuplicateAndInvalidRecords(t *testing.T) {
	valid := testRecord(testRecordA, "a.go", "A", []string{"a"})
	tests := []struct {
		name    string
		records []model.Record
	}{
		{name: "duplicate identity", records: []model.Record{valid, valid}},
		{name: "backwards range", records: []model.Record{func() model.Record { value := valid; value.EndLine = 0; return value }()}},
		{name: "absolute path", records: []model.Record{func() model.Record { value := valid; value.Path = "/a.go"; return value }()}},
		{name: "duplicate term", records: []model.Record{func() model.Record { value := valid; value.SearchTerms = []string{"a", "a"}; return value }()}},
		{name: "invalid identity", records: []model.Record{func() model.Record { value := valid; value.Identity = "record-a"; return value }()}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := encodeIndex(test.records); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("error = %v, want ErrInvalidIndex", err)
			}
		})
	}
}

func TestEncodeIndexRejectsOversizedInputBeforePerRecordAllocation(t *testing.T) {
	preview := strings.Repeat("p", maximumIndexStringBytes)
	sharedTerms := []string{"term"}
	record := testRecord(testRecordA, "oversized.go", "Oversized", sharedTerms)
	record.Preview = preview
	records := make([]model.Record, 25_000)
	for index := range records {
		records[index] = record
	}

	var encodeErr error
	allocations := testing.AllocsPerRun(1, func() {
		_, encodeErr = encodeIndex(records)
	})
	if !errors.Is(encodeErr, ErrInvalidIndex) {
		t.Fatalf("encode error = %v, want ErrInvalidIndex", encodeErr)
	}
	if allocations > 32 {
		t.Fatalf("oversized preflight allocations = %.0f, want <= 32", allocations)
	}
}

func TestEncodeIndexUsesLinearBoundedV2Representations(t *testing.T) {
	sharedTerms := []string{"term-b", "term-a"}
	records := make([]model.Record, 20_000)
	for index := range records {
		records[index] = testRecord(fmt.Sprintf("sha256:%064x", index), "bounded.go", "Bounded", sharedTerms)
	}

	var encodeErr error
	allocations := testing.AllocsPerRun(1, func() {
		_, encodeErr = encodeIndex(records)
	})
	if encodeErr != nil {
		t.Fatal(encodeErr)
	}
	// Format v2 derives canonical Unicode query keys twice (preflight and
	// ordinal fill) and grows the persisted facet postings linearly. This locks
	// the linear shape without pretending the auxiliary index is allocation-free.
	if allocations > 1_000_000 {
		t.Fatalf("valid v2 encode allocations = %.0f, want <= 1000000", allocations)
	}
}

func TestEncodeIndexRejectsMillionUniqueTermsBeforeCanonicalPostingSort(t *testing.T) {
	const termCount = 1_000_000
	records := make([]model.Record, termCount/maximumTermsPerRecord)
	nextTerm := 0
	for recordIndex := range records {
		terms := make([]string, maximumTermsPerRecord)
		for termIndex := range terms {
			terms[termIndex] = fmt.Sprintf("term-%027d", nextTerm)
			nextTerm++
		}
		records[recordIndex] = testRecord(
			fmt.Sprintf("sha256:%064x", recordIndex),
			fmt.Sprintf("bulk/%05d.go", recordIndex),
			fmt.Sprintf("Bulk%05d", recordIndex),
			terms,
		)
	}
	if nextTerm != termCount {
		t.Fatalf("fixture terms = %d, want %d", nextTerm, termCount)
	}

	postingSorts := 0
	_, err := encodeIndexObserved(records, func() { postingSorts++ })
	if !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("encode error = %v, want ErrInvalidIndex", err)
	}
	if postingSorts != 0 {
		t.Fatalf("canonical posting sorts = %d, want 0", postingSorts)
	}
}

func TestEncodeIndexPreflightAcceptsSharedTermsAcrossRecordsExactly(t *testing.T) {
	records := []model.Record{
		testRecord(testRecordB, "b.go", "B", []string{"unique-b", "shared"}),
		testRecord(testRecordA, "a.go", "A", []string{"shared", "unique-a"}),
	}
	want := cloneRecords(records)
	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	decoded, postings, err := decodeIndex(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if len(decoded) != 2 || len(postings) != 3 ||
		!slices.Equal(postings["shared"], []uint32{0, 1}) ||
		!slices.Equal(postings["unique-a"], []uint32{0}) ||
		!slices.Equal(postings["unique-b"], []uint32{1}) {
		t.Fatalf("decoded=%#v postings=%#v", decoded, postings)
	}
	if !recordsEqual(records, want) {
		t.Fatal("encodeIndex mutated caller-owned shared terms")
	}
}

func TestDecodeIndexRejectsVersionLengthsTrailingDataAndBombs(t *testing.T) {
	valid, err := encodeIndex([]model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name string
		data []byte
	}{
		{name: "unknown version", data: mutateHeader(valid, func(header []byte) { binary.BigEndian.PutUint16(header[len(indexMagic):], indexFormatVersion+1) })},
		{name: "trailing compressed bytes", data: append(slices.Clone(valid), 0)},
		{name: "truncated", data: slices.Clone(valid[:len(valid)-1])},
		{name: "malformed top-level length", data: compressedIndex(t, func(writer *zlib.Writer) {
			_, _ = writer.Write(indexMagic)
			_ = binary.Write(writer, binary.BigEndian, indexFormatVersion)
			_ = binary.Write(writer, binary.BigEndian, uint32(1))
			_ = binary.Write(writer, binary.BigEndian, uint32(maximumIndexStringBytes+1))
		})},
		{name: "decompression bomb", data: compressedBytes(t, bytes.Repeat([]byte{'x'}, maximumDecompressedIndexBytes+1))},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, _, err := decodeIndex(test.data); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("error = %v, want ErrInvalidIndex", err)
			}
		})
	}
}

func TestDecodeIndexRejectsNoncanonicalRecordsAndPostings(t *testing.T) {
	a := testRecord(testRecordA, "a.go", "A", []string{"a"})
	b := testRecord(testRecordB, "b.go", "B", []string{"b"})
	ab := testRecord(testRecordA, "a.go", "A", []string{"a", "b"})
	empty := testRecord(testRecordA, "a.go", "A", nil)
	invalidRange := a
	invalidRange.StartLine = 0
	unsortedTerms := ab
	unsortedTerms.SearchTerms = []string{"b", "a"}
	duplicateTerms := ab
	duplicateTerms.SearchTerms = []string{"a", "a"}
	tests := []struct {
		name     string
		records  []model.Record
		postings []postingFixture
	}{
		{name: "duplicate record identity", records: []model.Record{a, a}, postings: []postingFixture{{term: "a", ordinals: []uint32{0, 1}}}},
		{name: "unsorted record identity", records: []model.Record{b, a}, postings: []postingFixture{{term: "a", ordinals: []uint32{1}}, {term: "b", ordinals: []uint32{0}}}},
		{name: "invalid record range", records: []model.Record{invalidRange}, postings: []postingFixture{{term: "a", ordinals: []uint32{0}}}},
		{name: "unsorted record terms", records: []model.Record{unsortedTerms}, postings: []postingFixture{{term: "a", ordinals: []uint32{0}}, {term: "b", ordinals: []uint32{0}}}},
		{name: "duplicate record terms", records: []model.Record{duplicateTerms}, postings: []postingFixture{{term: "a", ordinals: []uint32{0}}}},
		{name: "duplicate posting term", records: []model.Record{a}, postings: []postingFixture{{term: "a", ordinals: []uint32{0}}, {term: "a", ordinals: []uint32{0}}}},
		{name: "unsorted posting terms", records: []model.Record{ab}, postings: []postingFixture{{term: "b", ordinals: []uint32{0}}, {term: "a", ordinals: []uint32{0}}}},
		{name: "duplicate posting ordinal", records: []model.Record{a, testRecord(testRecordB, "b.go", "B", []string{"a"})}, postings: []postingFixture{{term: "a", ordinals: []uint32{0, 0}}}},
		{name: "out of range posting ordinal", records: []model.Record{a}, postings: []postingFixture{{term: "a", ordinals: []uint32{1}}}},
		{name: "missing posting", records: []model.Record{a}},
		{name: "extra posting", records: []model.Record{empty}, postings: []postingFixture{{term: "a", ordinals: []uint32{0}}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, _, err := decodeIndex(rawIndex(t, test.records, test.postings)); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("error = %v, want ErrInvalidIndex", err)
			}
		})
	}

	var overflow bytes.Buffer
	overflow.Write(indexMagic)
	writeUint16(&overflow, indexFormatVersion)
	writeUint32(&overflow, math.MaxUint32)
	if _, _, err := decodeIndex(mustCompress(overflow.Bytes())); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("overflow record count error = %v", err)
	}
	validPlain := rawIndexPlain([]model.Record{empty}, nil)
	validPlain = append(validPlain, 0)
	if _, _, err := decodeIndex(mustCompress(validPlain)); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("trailing plain byte error = %v", err)
	}
}

func TestDecodeIndexUsesSingleLinearPostingRepresentation(t *testing.T) {
	records := make([]model.Record, 2_000)
	for recordIndex := range records {
		terms := make([]string, maximumTermsPerRecord)
		for termIndex := range terms {
			terms[termIndex] = fmt.Sprintf("term-%04d-%02d", recordIndex, termIndex)
		}
		records[recordIndex] = testRecord(fmt.Sprintf("sha256:%064x", recordIndex), "linear.go", "Linear", terms)
	}
	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}

	var decodeErr error
	allocations := testing.AllocsPerRun(1, func() {
		_, _, decodeErr = decodeIndex(encoded)
	})
	if decodeErr != nil {
		t.Fatal(decodeErr)
	}
	if allocations > 1_200_000 {
		t.Fatalf("v2 decode allocations = %.0f, want <= 1200000", allocations)
	}
}

func TestValidateIndexWithoutMaterializationEnforcesCanonicalExactness(t *testing.T) {
	records := []model.Record{
		testRecord(testRecordA, "a.go", "A", []string{"a", "shared"}),
		testRecord(testRecordB, "b.go", "B", []string{"b", "shared"}),
	}
	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	if recordCount, postingCount, err := validateIndex(encoded); err != nil || recordCount != 2 || postingCount != 3 {
		t.Fatalf("validateIndex = %d, %d, %v", recordCount, postingCount, err)
	}
	invalid := rawIndex(t, records, []postingFixture{
		{term: "a", ordinals: []uint32{0}},
		{term: "b", ordinals: []uint32{1}},
		{term: "missing", ordinals: []uint32{0, 1}},
	})
	if _, _, err := validateIndex(invalid); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("invalid posting error = %v, want ErrInvalidIndex", err)
	}
}

func TestDecodeIndexRejectsHostileMaximumCountBeforeLargeRecordAllocation(t *testing.T) {
	plain := make([]byte, len(indexMagic)+2+4+maximumIndexRecords*minimumEncodedRecordBytes())
	copy(plain, indexMagic)
	binary.BigEndian.PutUint16(plain[len(indexMagic):], indexFormatVersion)
	binary.BigEndian.PutUint32(plain[len(indexMagic)+2:], maximumIndexRecords)
	encoded := mustCompress(plain)
	plain = nil
	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	if _, _, err := decodeIndex(encoded); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("decode error = %v, want ErrInvalidIndex", err)
	}
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	// Race instrumentation increases zlib and slice-allocation accounting by
	// about 32 MiB. The eager million-record allocation exceeded 300 MiB even
	// without race instrumentation.
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 256<<20 {
		t.Fatalf("hostile decode allocated %d bytes, want <= %d", allocated, 256<<20)
	}
}

func TestDecodeMemoryBudgetCoversRecordSliceGrowth(t *testing.T) {
	minimum := 2 * int(unsafe.Sizeof(model.Record{}))
	if conservativeRecordMemoryBytes < minimum {
		t.Fatalf("record memory budget = %d, want at least %d", conservativeRecordMemoryBytes, minimum)
	}
}

func TestDecodeMemoryBudgetChargesStringAllocationOverhead(t *testing.T) {
	budget := decodeMemoryBudget{}
	decoder := binaryDecoder{value: []byte{0, 0, 0, 1, 'a'}, budget: &budget}
	if value, err := decoder.readString(1); err != nil || value != "a" {
		t.Fatalf("readString = %q, %v", value, err)
	}
	want := int64(1 + conservativeStringAllocationOverhead)
	if budget.used != want {
		t.Fatalf("string memory budget = %d, want %d", budget.used, want)
	}
}

func TestCanonicalManifestIsStableAndStrict(t *testing.T) {
	left := testManifest()
	left.ParserIdentities = map[string]string{"python": "tree-sitter-python@0.25.0", "go": "go/parser@go1.27"}
	left.Coverage.ExclusionReasonCounts = map[string]int{"vendored": 2, "generated": 1}
	right := left
	right.ParserIdentities = map[string]string{"go": "go/parser@go1.27", "python": "tree-sitter-python@0.25.0"}
	right.Coverage.ExclusionReasonCounts = map[string]int{"generated": 1, "vendored": 2}

	leftBytes, err := encodeManifest(left)
	if err != nil {
		t.Fatal(err)
	}
	rightBytes, err := encodeManifest(right)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(leftBytes, rightBytes) {
		t.Fatalf("manifest map order changed bytes:\n%s\n%s", leftBytes, rightBytes)
	}
	decoded, err := decodeManifest(leftBytes)
	if err != nil {
		t.Fatal(err)
	}
	if decoded.EngineVersion != left.EngineVersion || decoded.Coverage.ExclusionReasonCounts["vendored"] != 2 {
		t.Fatalf("decoded manifest = %#v", decoded)
	}

	noncanonical := append([]byte(" "), leftBytes...)
	if _, err := decodeManifest(noncanonical); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("noncanonical error = %v", err)
	}
	duplicate := bytes.Replace(leftBytes, []byte(`"engine_version":"engine-v1"`), []byte(`"engine_version":"engine-v1","engine_version":"engine-v1"`), 1)
	if _, err := decodeManifest(duplicate); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("duplicate-key error = %v", err)
	}
	invalid := left
	invalid.Coverage.PathCoverage = math.NaN()
	if _, err := encodeManifest(invalid); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("nonfinite error = %v", err)
	}
	invalid.Coverage.PathCoverage = math.Copysign(0, -1)
	if _, err := encodeManifest(invalid); !errors.Is(err, ErrInvalidManifest) {
		t.Fatalf("negative-zero error = %v", err)
	}
}

func TestBuildLoadInspectAreContentAddressedDeterministicAndExact(t *testing.T) {
	leftRoots, leftState := storeRoots(t)
	rightRoots, rightState := storeRoots(t)
	records := []model.Record{
		testRecord(testRecordB, "z/service.go", "z.Service", []string{"service", "run"}),
		testRecord(testRecordA, "a/service.go", "a.Service", []string{"service", "api"}),
	}
	permuted := []model.Record{cloneRecord(records[1]), cloneRecord(records[0])}
	permuted[0].SearchTerms = []string{"api", "service"}
	permuted[1].SearchTerms = []string{"run", "service"}

	left := mustBuild(t, leftRoots, testManifest(), records)
	right := mustBuild(t, rightRoots, testManifest(), permuted)
	t.Logf("index_identity=%s generation_identity=%s installed_bytes=%d", left.IndexIdentity, left.Manifest.GenerationIdentity, left.InstalledBytes)
	if left.IndexIdentity != right.IndexIdentity || left.Manifest.GenerationIdentity != right.Manifest.GenerationIdentity {
		t.Fatalf("identities differ: left=%#v right=%#v", left, right)
	}
	leftBytes := generationBytes(t, leftState, left.Manifest.GenerationIdentity)
	rightBytes := generationBytes(t, rightState, right.Manifest.GenerationIdentity)
	if !bytes.Equal(leftBytes, rightBytes) {
		t.Fatal("installed generation bytes changed with input/map order")
	}
	indexBytes := installedFile(t, leftState, left.Manifest.GenerationIdentity, indexFilename)
	if got := digestIdentity(indexBytes); got != left.IndexIdentity || left.Manifest.PayloadDigest != got {
		t.Fatalf("payload identities = %q %q, want %q", left.IndexIdentity, left.Manifest.PayloadDigest, got)
	}
	wantInstalled := int64(len(indexBytes) + len(installedFile(t, leftState, left.Manifest.GenerationIdentity, manifestFilename)) + len(installedFile(t, leftState, left.Manifest.GenerationIdentity, readyFilename)))
	if left.InstalledBytes != wantInstalled {
		t.Fatalf("InstalledBytes = %d, want %d", left.InstalledBytes, wantInstalled)
	}
	loaded, err := Load(leftRoots, left.IndexIdentity)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(loaded, left) {
		t.Fatalf("loaded snapshot differs:\ngot  %#v\nwant %#v", loaded, left)
	}
	status, err := Inspect(leftRoots)
	if err != nil {
		t.Fatal(err)
	}
	if !status.Ready || status.IndexIdentity != left.IndexIdentity || status.GenerationIdentity != left.Manifest.GenerationIdentity || status.InstalledBytes != left.InstalledBytes {
		t.Fatalf("status = %#v", status)
	}

	repeated := mustBuild(t, leftRoots, testManifest(), records)
	if !reflect.DeepEqual(repeated, left) || !bytes.Equal(generationBytes(t, leftState, left.Manifest.GenerationIdentity), leftBytes) {
		t.Fatal("idempotent build changed snapshot or installed generation bytes")
	}
}

func TestBuildMaterializesOnlyTheReturnedGeneration(t *testing.T) {
	recordA := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	recordB := []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}

	t.Run("new generation", func(t *testing.T) {
		roots, _ := storeRoots(t)
		count := 0
		snapshot, err := buildWithFilesystemObserved(boundaryFilesystem{}, roots, testManifest(), recordA, buildHooks{materialized: func() { count++ }})
		if err != nil || snapshot.IndexIdentity == "" {
			t.Fatalf("Build = %#v, %v", snapshot, err)
		}
		if count != 1 {
			t.Fatalf("materializations = %d, want 1", count)
		}
	})

	t.Run("same current", func(t *testing.T) {
		roots, _ := storeRoots(t)
		first := mustBuild(t, roots, testManifest(), recordA)
		count := 0
		snapshot, err := buildWithFilesystemObserved(boundaryFilesystem{}, roots, testManifest(), recordA, buildHooks{materialized: func() { count++ }})
		if err != nil || snapshot.IndexIdentity != first.IndexIdentity {
			t.Fatalf("Build = %#v, %v", snapshot, err)
		}
		if count != 1 {
			t.Fatalf("materializations = %d, want 1", count)
		}
	})

	t.Run("existing immutable generation reuse", func(t *testing.T) {
		roots, _ := storeRoots(t)
		first := mustBuild(t, roots, testManifest(), recordA)
		mustBuild(t, roots, manifestVariant("b"), recordB)
		count := 0
		snapshot, err := buildWithFilesystemObserved(boundaryFilesystem{}, roots, testManifest(), recordA, buildHooks{materialized: func() { count++ }})
		if err != nil || snapshot.IndexIdentity != first.IndexIdentity {
			t.Fatalf("Build = %#v, %v", snapshot, err)
		}
		if count != 1 {
			t.Fatalf("materializations = %d, want 1", count)
		}
	})

	t.Run("rollback before return", func(t *testing.T) {
		roots, _ := storeRoots(t)
		mustBuild(t, roots, testManifest(), recordA)
		injected := errors.New("state sync failure")
		count := 0
		_, err := buildWithFilesystemObserved(
			boundaryFilesystem{faults: Faults{BeforeStateSync: injected}}, roots, manifestVariant("b"), recordB,
			buildHooks{materialized: func() { count++ }},
		)
		if !errors.Is(err, injected) {
			t.Fatalf("Build error = %v, want injected failure", err)
		}
		if count != 0 {
			t.Fatalf("materializations = %d, want 0", count)
		}
	})

	t.Run("corrupt current generation", func(t *testing.T) {
		roots, state := storeRoots(t)
		first := mustBuild(t, roots, testManifest(), recordA)
		writeExisting(t, installedPath(state, first.Manifest.GenerationIdentity, readyFilename), []byte("corrupt\n"))
		count := 0
		_, err := buildWithFilesystemObserved(boundaryFilesystem{}, roots, manifestVariant("b"), recordB, buildHooks{materialized: func() { count++ }})
		if !errors.Is(err, ErrStoreCorrupt) {
			t.Fatalf("Build error = %v, want ErrStoreCorrupt", err)
		}
		if count != 0 {
			t.Fatalf("materializations = %d, want 0", count)
		}
	})

	t.Run("generation collision", func(t *testing.T) {
		roots, state := storeRoots(t)
		mustBuild(t, roots, testManifest(), recordA)
		otherRoots, otherState := storeRoots(t)
		secondManifest := manifestVariant("b")
		second := mustBuild(t, otherRoots, secondManifest, recordB)
		copyGeneration(t, otherState, state, second.Manifest.GenerationIdentity)
		path := installedPath(state, second.Manifest.GenerationIdentity, indexFilename)
		data := mustRead(t, path)
		data[len(data)-1] ^= 0xff
		writeExisting(t, path, data)
		count := 0
		_, err := buildWithFilesystemObserved(boundaryFilesystem{}, roots, secondManifest, recordB, buildHooks{materialized: func() { count++ }})
		if !errors.Is(err, ErrGenerationCollision) {
			t.Fatalf("Build error = %v, want ErrGenerationCollision", err)
		}
		if count != 0 {
			t.Fatalf("materializations = %d, want 0", count)
		}
	})
}

func TestBuildReviewerShapePeakMemoryStaysBelowBudget(t *testing.T) {
	if raceEnabled {
		t.Skip("heap-peak accounting is a non-race resource regression")
	}
	const recordCount = 300_000
	records := make([]model.Record, recordCount)
	for index := range records {
		records[index] = model.Record{
			Identity:         fmt.Sprintf("sha256:%064x", index),
			Path:             "a",
			StartLine:        1,
			EndLine:          1,
			Language:         "g",
			RecordKind:       model.Module,
			SourceType:       "source",
			QualifiedName:    "a",
			ExtractionMethod: "a",
			EvidenceClass:    model.Verified,
			SourceDigest:     "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		}
	}
	preflight, err := preflightEncodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	if preflight.plainSize != 85_800_318 {
		t.Fatalf("reviewer-shape v2 preflight bytes = %d, want 85800318", preflight.plainSize)
	}

	for _, test := range []struct {
		name      string
		gcPercent int
	}{
		{name: "default GOGC", gcPercent: 100},
		{name: "GOGC=1", gcPercent: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			previousGC := debug.SetGCPercent(test.gcPercent)
			defer debug.SetGCPercent(previousGC)
			runtime.GC()
			var baseline runtime.MemStats
			runtime.ReadMemStats(&baseline)
			stop := make(chan struct{})
			peak := make(chan uint64, 1)
			go samplePeakHeap(stop, peak, baseline.HeapAlloc)
			roots, _ := storeRoots(t)
			snapshot, err := Build(roots, testManifest(), records)
			close(stop)
			used := <-peak
			if err != nil {
				t.Fatal(err)
			}
			if len(snapshot.Records) != recordCount {
				t.Fatalf("records = %d, want %d", len(snapshot.Records), recordCount)
			}
			t.Logf("Build peak heap = %d bytes", used)
			if used >= maximumIndexPeakBytes {
				t.Fatalf("Build peak heap = %d bytes, want < %d", used, maximumIndexPeakBytes)
			}
		})
	}
	runtime.KeepAlive(records)
}

func samplePeakHeap(stop <-chan struct{}, result chan<- uint64, baseline uint64) {
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	maximum := uint64(0)
	observe := func() {
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		if memory.HeapAlloc > baseline && memory.HeapAlloc-baseline > maximum {
			maximum = memory.HeapAlloc - baseline
		}
	}
	for {
		select {
		case <-stop:
			observe()
			result <- maximum
			return
		case <-ticker.C:
			observe()
		}
	}
}

func TestBuildRejectsUnknownManifestFormatAndControlCharacters(t *testing.T) {
	t.Run("unknown manifest format", func(t *testing.T) {
		roots, state := storeRoots(t)
		manifest := testManifest()
		manifest.FormatVersion = "1"
		if _, err := Build(roots, manifest, []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}); !errors.Is(err, ErrInvalidManifest) {
			t.Fatalf("error = %v, want ErrInvalidManifest", err)
		}
		if _, err := os.Lstat(state); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("invalid input created state root: %v", err)
		}
	})
	t.Run("control character in path", func(t *testing.T) {
		roots, state := storeRoots(t)
		record := testRecord(testRecordA, "a\n.go", "A", []string{"a"})
		if _, err := Build(roots, testManifest(), []model.Record{record}); !errors.Is(err, ErrInvalidIndex) {
			t.Fatalf("error = %v, want ErrInvalidIndex", err)
		}
		if _, err := os.Lstat(state); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("invalid input created state root: %v", err)
		}
	})
	for _, test := range []struct {
		name   string
		mutate func(*model.Manifest)
	}{
		{name: "nil parser identities", mutate: func(manifest *model.Manifest) { manifest.ParserIdentities = nil }},
		{name: "nil exclusion reasons", mutate: func(manifest *model.Manifest) { manifest.Coverage.ExclusionReasonCounts = nil }},
	} {
		t.Run(test.name, func(t *testing.T) {
			roots, state := storeRoots(t)
			manifest := testManifest()
			test.mutate(&manifest)
			if _, err := Build(roots, manifest, []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}); !errors.Is(err, ErrInvalidManifest) {
				t.Fatalf("error = %v, want ErrInvalidManifest", err)
			}
			if _, err := os.Lstat(state); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("invalid input created state root: %v", err)
			}
		})
	}
}

func TestLoadAndInspectFailClosedOnCorruptionAndUnsafeState(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*testing.T, string, Snapshot)
	}{
		{name: "unsafe current generation", mutate: func(t *testing.T, state string, _ Snapshot) {
			writeExisting(t, filepath.Join(state, currentFilename), []byte("../escape\n"))
		}},
		{name: "current generation mismatch", mutate: func(t *testing.T, state string, _ Snapshot) {
			writeExisting(t, filepath.Join(state, currentFilename), []byte(strings.Repeat("f", 64)+"\n"))
		}},
		{name: "insecure current mode", mutate: func(t *testing.T, state string, _ Snapshot) {
			if err := os.Chmod(filepath.Join(state, currentFilename), 0o644); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "current symlink", mutate: func(t *testing.T, state string, _ Snapshot) {
			path := filepath.Join(state, currentFilename)
			data := mustRead(t, path)
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			outside := filepath.Join(t.TempDir(), "CURRENT")
			if err := os.WriteFile(outside, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, path); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "current hardlink", mutate: func(t *testing.T, state string, _ Snapshot) {
			if err := os.Link(filepath.Join(state, currentFilename), filepath.Join(state, "CURRENT-alias")); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "ready mismatch", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			writeExisting(t, installedPath(state, snapshot.Manifest.GenerationIdentity, readyFilename), []byte("sha256:"+strings.Repeat("f", 64)+"\n"))
		}},
		{name: "noncanonical manifest", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			path := installedPath(state, snapshot.Manifest.GenerationIdentity, manifestFilename)
			writeExisting(t, path, append([]byte(" "), mustRead(t, path)...))
		}},
		{name: "payload corruption", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			path := installedPath(state, snapshot.Manifest.GenerationIdentity, indexFilename)
			data := mustRead(t, path)
			writeExisting(t, path, data[:len(data)-1])
		}},
		{name: "insecure generation mode", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			if err := os.Chmod(generationPath(state, snapshot.Manifest.GenerationIdentity), 0o755); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "insecure manifest mode", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			if err := os.Chmod(installedPath(state, snapshot.Manifest.GenerationIdentity, manifestFilename), 0o644); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "generation symlink", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			path := generationPath(state, snapshot.Manifest.GenerationIdentity)
			retained := path + "-retained"
			if err := os.Rename(path, retained); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(retained, path); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "manifest symlink", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			path := installedPath(state, snapshot.Manifest.GenerationIdentity, manifestFilename)
			data := mustRead(t, path)
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			outside := filepath.Join(t.TempDir(), "manifest")
			if err := os.WriteFile(outside, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(outside, path); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "payload hardlink", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			path := installedPath(state, snapshot.Manifest.GenerationIdentity, indexFilename)
			data := mustRead(t, path)
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			outside := filepath.Join(t.TempDir(), "index")
			if err := os.WriteFile(outside, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Link(outside, path); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "unexpected generation entry", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			if err := os.WriteFile(installedPath(state, snapshot.Manifest.GenerationIdentity, "extra"), []byte("extra"), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			roots, state := storeRoots(t)
			snapshot := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
			test.mutate(t, state, snapshot)
			if _, err := Load(roots, snapshot.IndexIdentity); !errors.Is(err, ErrStoreCorrupt) {
				t.Fatalf("Load error = %v, want ErrStoreCorrupt", err)
			}
			if _, err := Inspect(roots); !errors.Is(err, ErrStoreCorrupt) {
				t.Fatalf("Inspect error = %v, want ErrStoreCorrupt", err)
			}
		})
	}
}

func TestLoadRejectsWrongExpectedIdentityAndMissingCurrent(t *testing.T) {
	roots, _ := storeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	state, err := roots.OpenStateDirectory("")
	if err != nil {
		t.Fatal(err)
	}
	if err := state.CreateDirectory(generationsDirectory); err != nil {
		t.Fatal(err)
	}
	generations, err := state.OpenDirectory(generationsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	if err := generations.CreateDirectory(".stage-incomplete"); err != nil {
		t.Fatal(err)
	}
	_ = generations.Close()
	_ = state.Close()
	if _, err := Load(roots, "sha256:"+strings.Repeat("f", 64)); !errors.Is(err, ErrNoCurrent) {
		t.Fatalf("missing CURRENT error = %v, want ErrNoCurrent", err)
	}
	if _, err := Inspect(roots); !errors.Is(err, ErrNoCurrent) {
		t.Fatalf("Inspect missing CURRENT error = %v, want ErrNoCurrent", err)
	}

	freshRoots, _ := storeRoots(t)
	snapshot := mustBuild(t, freshRoots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if _, err := Load(freshRoots, "sha256:"+strings.Repeat("f", 64)); !errors.Is(err, ErrIndexMismatch) {
		t.Fatalf("wrong identity error = %v, want ErrIndexMismatch", err)
	}
	if _, err := Load(freshRoots, "invalid"); !errors.Is(err, ErrIndexMismatch) {
		t.Fatalf("invalid identity error = %v, want ErrIndexMismatch", err)
	}
	if snapshot.IndexIdentity == "" {
		t.Fatal("empty index identity")
	}
}

func TestIncompleteStagingIsIgnoredWhenCurrentIsValid(t *testing.T) {
	roots, _ := storeRoots(t)
	snapshot := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	state, err := roots.OpenStateDirectory("")
	if err != nil {
		t.Fatal(err)
	}
	generations, err := state.OpenDirectory(generationsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	if err := generations.CreateDirectory(".stage-incomplete"); err != nil {
		t.Fatal(err)
	}
	_ = generations.Close()
	_ = state.Close()
	loaded, err := Load(roots, snapshot.IndexIdentity)
	if err != nil || loaded.IndexIdentity != snapshot.IndexIdentity {
		t.Fatalf("Load = %#v, %v", loaded, err)
	}
}

func TestBuildFailsClosedOnCorruptCurrentAndPreservesPointer(t *testing.T) {
	roots, state := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	ready := installedPath(state, first.Manifest.GenerationIdentity, readyFilename)
	writeExisting(t, ready, []byte("corrupt\n"))
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	if _, err := Build(roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("Build error = %v, want ErrStoreCorrupt", err)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatal("Build over corrupt state changed CURRENT")
	}
}

func TestBuildDoesNotCreateGenerationsAroundDanglingCurrent(t *testing.T) {
	roots, state := storeRoots(t)
	if err := roots.EnsureState(); err != nil {
		t.Fatal(err)
	}
	current := []byte(strings.Repeat("f", 64) + "\n")
	if err := os.WriteFile(filepath.Join(state, currentFilename), current, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Build(roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("Build error = %v, want ErrStoreCorrupt", err)
	}
	if _, err := os.Stat(filepath.Join(state, generationsDirectory)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("Build mutated corrupt state: %v", err)
	}
	if got := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(got, current) {
		t.Fatal("Build changed dangling CURRENT")
	}
}

func TestBuildRejectsExistingGenerationCollisionAndKeepsPrevious(t *testing.T) {
	roots, state := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	otherRoots, otherState := storeRoots(t)
	secondManifest := manifestVariant("b")
	secondRecords := []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}
	second := mustBuild(t, otherRoots, secondManifest, secondRecords)
	copyGeneration(t, otherState, state, second.Manifest.GenerationIdentity)
	path := installedPath(state, second.Manifest.GenerationIdentity, indexFilename)
	data := mustRead(t, path)
	data[len(data)-1] ^= 0xff
	writeExisting(t, path, data)

	if _, err := Build(roots, secondManifest, secondRecords); !errors.Is(err, ErrGenerationCollision) {
		t.Fatalf("Build collision error = %v, want ErrGenerationCollision", err)
	}
	loaded, err := Load(roots, first.IndexIdentity)
	if err != nil || loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("previous generation = %#v, %v", loaded, err)
	}
}

func TestBuildFaultsPreservePreviousCurrentGeneration(t *testing.T) {
	injected := errors.New("injected")
	tests := []struct {
		name   string
		faults Faults
	}{
		{name: "payload sync", faults: Faults{BeforePayloadSync: injected}},
		{name: "payload reopen", faults: Faults{BeforePayloadReopen: injected}},
		{name: "manifest sync", faults: Faults{BeforeManifestSync: injected}},
		{name: "manifest reopen", faults: Faults{BeforeManifestReopen: injected}},
		{name: "ready sync", faults: Faults{BeforeReadySync: injected}},
		{name: "generation directory sync", faults: Faults{BeforeGenerationSync: injected}},
		{name: "generation rename", faults: Faults{BeforeGenerationRename: injected}},
		{name: "generations directory sync", faults: Faults{BeforeGenerationsSync: injected}},
		{name: "current sync", faults: Faults{BeforeCurrentSync: injected}},
		{name: "current reopen", faults: Faults{BeforeCurrentReopen: injected}},
		{name: "current rename", faults: Faults{BeforeCurrentRename: injected}},
		{name: "state root sync", faults: Faults{BeforeStateSync: injected}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			roots, state := storeRoots(t)
			first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
			currentBefore := mustRead(t, filepath.Join(state, currentFilename))
			_, err := BuildWithFaults(roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}, test.faults)
			if !errors.Is(err, injected) {
				t.Fatalf("BuildWithFaults error = %v, want injected", err)
			}
			if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
				t.Fatalf("CURRENT changed: before=%q after=%q", currentBefore, currentAfter)
			}
			assertNoTemporaryEntries(t, state)
			loaded, err := Load(roots, first.IndexIdentity)
			if err != nil || loaded.IndexIdentity != first.IndexIdentity {
				t.Fatalf("previous generation = %#v, %v", loaded, err)
			}
		})
	}
}

func TestBuildRollsBackIndeterminateCurrentRenameFailure(t *testing.T) {
	roots, state := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	injected := errors.New("error reported after current rename")
	filesystem := errorAfterCurrentRenameFilesystem{storeFilesystem: boundaryFilesystem{}, cause: injected}
	_, err := buildWithFilesystem(filesystem, roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})})
	if !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("build error = %v, want ErrStoreCorrupt", err)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed after indeterminate rename: before=%q after=%q", currentBefore, currentAfter)
	}
	assertNoTemporaryEntries(t, state)
	loaded, err := Load(roots, first.IndexIdentity)
	if err != nil || loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("previous generation = %#v, %v", loaded, err)
	}
}

func TestBuildSyncsGenerationAfterIndeterminateRename(t *testing.T) {
	t.Run("sync succeeds before current publication", func(t *testing.T) {
		roots, _ := storeRoots(t)
		mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
		syncCalls := 0
		filesystem := errorAfterGenerationRenameFilesystem{
			storeFilesystem: boundaryFilesystem{},
			cause:           errors.New("error reported after generation rename"),
			generationSyncs: &syncCalls,
		}
		snapshot, err := buildWithFilesystem(filesystem, roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})})
		if err != nil {
			t.Fatal(err)
		}
		if snapshot.IndexIdentity == "" || syncCalls != 1 {
			t.Fatalf("snapshot=%#v generation sync calls=%d, want 1", snapshot, syncCalls)
		}
	})

	t.Run("sync failure preserves previous current", func(t *testing.T) {
		roots, state := storeRoots(t)
		first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
		currentBefore := mustRead(t, filepath.Join(state, currentFilename))
		syncFailure := errors.New("generation sync failed")
		syncCalls := 0
		filesystem := errorAfterGenerationRenameFilesystem{
			storeFilesystem: boundaryFilesystem{faults: Faults{BeforeGenerationsSync: syncFailure}},
			cause:           errors.New("error reported after generation rename"),
			generationSyncs: &syncCalls,
		}
		_, err := buildWithFilesystem(filesystem, roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})})
		if !errors.Is(err, syncFailure) {
			t.Fatalf("build error = %v, want generation sync failure", err)
		}
		if syncCalls != 1 {
			t.Fatalf("generation sync calls = %d, want 1", syncCalls)
		}
		if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
			t.Fatalf("CURRENT changed: before=%q after=%q", currentBefore, currentAfter)
		}
		assertNoTemporaryEntries(t, state)
		loaded, loadErr := Load(roots, first.IndexIdentity)
		if loadErr != nil || loaded.IndexIdentity != first.IndexIdentity {
			t.Fatalf("previous generation = %#v, %v", loaded, loadErr)
		}
	})
}

func TestConcurrentBuilderAndInspectorsExposeOnlyReadyGenerations(t *testing.T) {
	roots, _ := storeRoots(t)
	firstManifest := testManifest()
	firstRecords := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	first := mustBuild(t, roots, firstManifest, firstRecords)
	secondManifest := manifestVariant("b")
	secondRecords := []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}
	second, err := prepareGeneration(secondManifest, secondRecords)
	if err != nil {
		t.Fatal(err)
	}
	start := make(chan struct{})
	errorsSeen := make(chan error, 16)
	var wait sync.WaitGroup
	wait.Add(1)
	go func() {
		defer wait.Done()
		<-start
		for range 20 {
			snapshot, err := Build(roots, secondManifest, secondRecords)
			if err != nil {
				errorsSeen <- fmt.Errorf("builder: %w", err)
				return
			}
			if snapshot.IndexIdentity != second.manifestValue.IndexIdentity {
				errorsSeen <- errors.New("builder returned wrong index")
				return
			}
			snapshot, err = Build(roots, firstManifest, firstRecords)
			if err != nil {
				errorsSeen <- fmt.Errorf("builder: %w", err)
				return
			}
			if snapshot.IndexIdentity != first.IndexIdentity {
				errorsSeen <- errors.New("builder returned wrong restored index")
				return
			}
		}
	}()
	for range 4 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			for range 200 {
				status, err := Inspect(roots)
				if err != nil {
					errorsSeen <- fmt.Errorf("inspector: %w", err)
					return
				}
				if status.IndexIdentity != first.IndexIdentity && status.IndexIdentity != second.manifestValue.IndexIdentity {
					errorsSeen <- errors.New("inspector observed unknown index")
					return
				}
			}
		}()
	}
	close(start)
	wait.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		t.Error(err)
	}
	loaded, err := Load(roots, first.IndexIdentity)
	if err != nil || loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("final generation = %#v, %v", loaded, err)
	}
}

func TestAtomicCurrentExtremeChurnNeverFalseCorrupts(t *testing.T) {
	roots, state := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	second := mustBuild(t, roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})})
	mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	currentValues := [][]byte{
		[]byte(generationToken(first.Manifest.GenerationIdentity) + "\n"),
		[]byte(generationToken(second.Manifest.GenerationIdentity) + "\n"),
	}

	start := make(chan struct{})
	done := make(chan struct{})
	errorsSeen := make(chan error, 16)
	var wait sync.WaitGroup
	wait.Add(1)
	go func() {
		defer wait.Done()
		defer close(done)
		<-start
		for iteration := range 50_000 {
			temporary := filepath.Join(state, fmt.Sprintf(".CURRENT-churn-%d", iteration))
			if err := os.WriteFile(temporary, currentValues[iteration&1], 0o600); err != nil {
				errorsSeen <- fmt.Errorf("write churn pointer: %w", err)
				return
			}
			if err := os.Rename(temporary, filepath.Join(state, currentFilename)); err != nil {
				errorsSeen <- fmt.Errorf("rename churn pointer: %w", err)
				return
			}
		}
	}()
	for range 8 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			for {
				select {
				case <-done:
					return
				default:
				}
				status, err := Inspect(roots)
				if err != nil {
					errorsSeen <- fmt.Errorf("inspect during churn: %w", err)
					return
				}
				if status.IndexIdentity != first.IndexIdentity && status.IndexIdentity != second.IndexIdentity {
					errorsSeen <- fmt.Errorf("inspect observed unknown index %q", status.IndexIdentity)
					return
				}
			}
		}()
	}
	close(start)
	wait.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		t.Error(err)
	}
}

func FuzzDecodeIndex(f *testing.F) {
	valid, err := encodeIndex([]model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if err != nil {
		f.Fatal(err)
	}
	f.Add([]byte{})
	f.Add([]byte("TAF"))
	f.Add(valid)
	f.Add(compressedBytes(f, bytes.Repeat([]byte{'x'}, 1024)))
	f.Fuzz(func(t *testing.T, data []byte) {
		_, _, _ = decodeIndex(data)
		_, _, _ = validateIndex(data)
	})
}

func FuzzMalformedRawV2Bounded(f *testing.F) {
	valid, err := encodeIndex([]model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if err != nil {
		f.Fatal(err)
	}
	f.Add(valid)
	f.Add([]byte{})
	f.Add(mutateHeader(valid, func(plain []byte) {
		binary.BigEndian.PutUint16(plain[len(indexMagic):], indexFormatVersion-1)
	}))
	f.Fuzz(func(t *testing.T, data []byte) {
		if len(data) > 1<<20 {
			t.Skip()
		}
		records, postings, _, decodeErr := decodeIndexContext(context.Background(), data)
		recordCount, postingCount, validateErr := validateIndexContext(context.Background(), data)
		if (decodeErr == nil) != (validateErr == nil) {
			t.Fatalf("decoder/validator disagree: decode=%v validate=%v", decodeErr, validateErr)
		}
		if decodeErr == nil && (recordCount != len(records) || postingCount != len(postings)) {
			t.Fatalf("raw v2 counts = (%d,%d), materialized=(%d,%d)", recordCount, postingCount, len(records), len(postings))
		}
	})
}

type fataler interface {
	Helper()
	Fatal(...any)
}

type postingFixture struct {
	term     string
	ordinals []uint32
}

type errorAfterCurrentRenameFilesystem struct {
	storeFilesystem
	cause error
}

type errorAfterGenerationRenameFilesystem struct {
	storeFilesystem
	cause           error
	generationSyncs *int
}

func (filesystem errorAfterGenerationRenameFilesystem) renameNew(directory *boundary.StateDirectory, source, destination string, point faultPoint) error {
	if err := filesystem.storeFilesystem.renameNew(directory, source, destination, point); err != nil {
		return err
	}
	if point == faultBeforeGenerationRename {
		return filesystem.cause
	}
	return nil
}

func (filesystem errorAfterGenerationRenameFilesystem) syncDirectory(directory *boundary.StateDirectory, point faultPoint) error {
	if point == faultBeforeGenerationsSync {
		*filesystem.generationSyncs++
	}
	return filesystem.storeFilesystem.syncDirectory(directory, point)
}

func (filesystem errorAfterCurrentRenameFilesystem) replaceFile(directory *boundary.StateDirectory, source, destination string, point faultPoint) error {
	if err := filesystem.storeFilesystem.replaceFile(directory, source, destination, point); err != nil {
		return err
	}
	if point == faultBeforeCurrentRename {
		return filesystem.cause
	}
	return nil
}

func rawIndex(t *testing.T, records []model.Record, postings []postingFixture) []byte {
	t.Helper()
	return mustCompress(rawIndexPlain(records, postings))
}

func rawIndexPlain(records []model.Record, postings []postingFixture) []byte {
	var plain bytes.Buffer
	plain.Write(indexMagic)
	writeUint16(&plain, indexFormatVersion)
	writeUint32(&plain, uint32(len(records)))
	for _, record := range records {
		writeRecord(&plain, record)
	}
	writeUint32(&plain, uint32(len(postings)))
	for _, posting := range postings {
		writeString(&plain, posting.term)
		writeUint32(&plain, uint32(len(posting.ordinals)))
		for _, ordinal := range posting.ordinals {
			writeUint32(&plain, ordinal)
		}
	}
	return plain.Bytes()
}

func compressedBytes(t fataler, plain []byte) []byte {
	t.Helper()
	var output bytes.Buffer
	writer, err := zlib.NewWriterLevel(&output, zlib.BestCompression)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write(plain); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func compressedIndex(t *testing.T, write func(*zlib.Writer)) []byte {
	t.Helper()
	var output bytes.Buffer
	writer, err := zlib.NewWriterLevel(&output, zlib.BestCompression)
	if err != nil {
		t.Fatal(err)
	}
	write(writer)
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func mutateHeader(encoded []byte, mutate func([]byte)) []byte {
	reader, err := zlib.NewReader(bytes.NewReader(encoded))
	if err != nil {
		panic(err)
	}
	var plain bytes.Buffer
	_, _ = plain.ReadFrom(reader)
	_ = reader.Close()
	value := plain.Bytes()
	mutate(value)
	return mustCompress(value)
}

func mustDecompress(t *testing.T, encoded []byte) []byte {
	t.Helper()
	reader, err := zlib.NewReader(bytes.NewReader(encoded))
	if err != nil {
		t.Fatal(err)
	}
	plain, err := io.ReadAll(reader)
	if closeErr := reader.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		t.Fatal(err)
	}
	return plain
}

func mutateFirstQueryOrdinal(t *testing.T, encoded []byte, ordinal uint32) []byte {
	t.Helper()
	plain := mustDecompress(t, encoded)
	decoder := rawBinaryDecoder{value: plain}
	_, _ = decoder.readBytes(len(indexMagic))
	_, _ = decoder.readUint16()
	recordCount, err := decoder.readCount(maximumIndexRecords)
	if err != nil {
		t.Fatal(err)
	}
	for range recordCount {
		if err := skipRawRecord(&decoder); err != nil {
			t.Fatal(err)
		}
	}
	postingCount, err := decoder.readCount(maximumPostingTerms)
	if err != nil {
		t.Fatal(err)
	}
	for range postingCount {
		if _, err := decoder.readString(128); err != nil {
			t.Fatal(err)
		}
		count, err := decoder.readCount(maximumPostingOrdinals)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := decoder.readBytes(count * 4); err != nil {
			t.Fatal(err)
		}
	}
	queryPostingCount, err := decoder.readCount(maximumQueryPostingTerms)
	if err != nil || queryPostingCount == 0 {
		t.Fatalf("query posting count = %d, err=%v", queryPostingCount, err)
	}
	if _, err := decoder.readString(maximumQueryKeyBytes); err != nil {
		t.Fatal(err)
	}
	ordinalCount, err := decoder.readCount(maximumQueryPostingOrdinals)
	if err != nil || ordinalCount == 0 {
		t.Fatalf("query ordinal count = %d, err=%v", ordinalCount, err)
	}
	rangeCount, err := decoder.readCount(ordinalCount)
	if err != nil || rangeCount == 0 {
		t.Fatalf("query range count = %d, err=%v", rangeCount, err)
	}
	binary.BigEndian.PutUint32(plain[decoder.offset:decoder.offset+4], ordinal)
	return mustCompress(plain)
}

func mustCompress(plain []byte) []byte {
	var output bytes.Buffer
	writer, err := zlib.NewWriterLevel(&output, zlib.BestCompression)
	if err != nil {
		panic(err)
	}
	_, _ = writer.Write(plain)
	if err := writer.Close(); err != nil {
		panic(err)
	}
	return output.Bytes()
}

func testRecord(identity, path, qualified string, terms []string) model.Record {
	return model.Record{
		Identity: identity, Path: path, StartLine: 1, EndLine: 2,
		Language: "go", RecordKind: model.Definition, SourceType: "source",
		QualifiedName: qualified, ExtractionMethod: "go/parser@go1.27",
		EvidenceClass: model.Verified, SearchTerms: slices.Clone(terms),
		SourceDigest: "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
	}
}

func testManifest() model.Manifest {
	return model.Manifest{
		FormatVersion: "2", EngineVersion: "engine-v1",
		Binding: model.Binding{
			RepositoryIdentity:      "sha256:1111111111111111111111111111111111111111111111111111111111111111",
			WorktreeIdentity:        "sha256:2222222222222222222222222222222222222222222222222222222222222222",
			CommittedHead:           strings.Repeat("3", 40),
			DirtyOverlayFingerprint: "sha256:4444444444444444444444444444444444444444444444444444444444444444",
		},
		InclusionPolicyIdentity: "sha256:5555555555555555555555555555555555555555555555555555555555555555",
		ExclusionPolicyIdentity: "sha256:6666666666666666666666666666666666666666666666666666666666666666",
		ParserIdentities:        map[string]string{}, Coverage: model.Coverage{ExclusionReasonCounts: map[string]int{}},
		SourceBindingDigest: "sha256:7777777777777777777777777777777777777777777777777777777777777777",
		SemanticDigest:      "sha256:8888888888888888888888888888888888888888888888888888888888888888",
		PayloadDigest:       "sha256:9999999999999999999999999999999999999999999999999999999999999999",
		IndexIdentity:       "sha256:9999999999999999999999999999999999999999999999999999999999999999",
		GenerationIdentity:  "sha256:0000000000000000000000000000000000000000000000000000000000000000",
	}
}

func manifestVariant(value string) model.Manifest {
	manifest := testManifest()
	manifest.Binding.CommittedHead = strings.Repeat(value, 40)
	manifest.SourceBindingDigest = "sha256:" + strings.Repeat(value, 64)
	manifest.SemanticDigest = "sha256:" + strings.Repeat(value, 64)
	return manifest
}

func storeRoots(t *testing.T) (*boundary.Roots, string) {
	t.Helper()
	repository := filepath.Join(t.TempDir(), "repository")
	if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(t.TempDir(), "state")
	roots, err := boundary.ValidateRoots(wire.Envelope{RepositoryRoot: repository, StateRoot: state})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = roots.Close() })
	return &roots, state
}

func mustBuild(t *testing.T, roots *boundary.Roots, manifest model.Manifest, records []model.Record) Snapshot {
	t.Helper()
	snapshot, err := Build(roots, manifest, records)
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func generationToken(identity string) string { return strings.TrimPrefix(identity, "sha256:") }

func generationPath(state, identity string) string {
	return filepath.Join(state, generationsDirectory, generationToken(identity))
}

func installedPath(state, identity, name string) string {
	return filepath.Join(generationPath(state, identity), name)
}

func installedFile(t *testing.T, state, identity, name string) []byte {
	t.Helper()
	return mustRead(t, installedPath(state, identity, name))
}

func generationBytes(t *testing.T, state, identity string) []byte {
	t.Helper()
	var result []byte
	for _, name := range []string{indexFilename, manifestFilename, readyFilename} {
		value := installedFile(t, state, identity, name)
		var length [8]byte
		binary.BigEndian.PutUint64(length[:], uint64(len(value)))
		result = append(result, length[:]...)
		result = append(result, value...)
	}
	return result
}

func digestIdentity(value []byte) string {
	digest := sha256.Sum256(value)
	return "sha256:" + hex.EncodeToString(digest[:])
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	value, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func writeExisting(t *testing.T, path string, value []byte) {
	t.Helper()
	if err := os.WriteFile(path, value, 0o600); err != nil {
		t.Fatal(err)
	}
}

func copyGeneration(t *testing.T, sourceState, destinationState, identity string) {
	t.Helper()
	destination := generationPath(destinationState, identity)
	if err := os.Mkdir(destination, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{indexFilename, manifestFilename, readyFilename} {
		if err := os.WriteFile(filepath.Join(destination, name), installedFile(t, sourceState, identity, name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func assertNoTemporaryEntries(t *testing.T, state string) {
	t.Helper()
	entries, err := os.ReadDir(state)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".CURRENT-") {
			t.Fatalf("temporary current entry retained: %s", entry.Name())
		}
	}
	generationEntries, err := os.ReadDir(filepath.Join(state, generationsDirectory))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range generationEntries {
		if strings.HasPrefix(entry.Name(), ".stage-") {
			t.Fatalf("staging entry retained: %s", entry.Name())
		}
	}
}

func cloneRecord(record model.Record) model.Record {
	record.SearchTerms = slices.Clone(record.SearchTerms)
	return record
}

func cloneRecords(records []model.Record) []model.Record {
	result := make([]model.Record, len(records))
	for index, record := range records {
		result[index] = cloneRecord(record)
	}
	return result
}

func recordsEqual(left, right []model.Record) bool {
	return reflect.DeepEqual(left, right)
}
