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
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
	"unsafe"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
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

func TestPrepareGenerationValidatesInputsWhileEncodingAndDoesNotRevalidateThePayload(t *testing.T) {
	// Pins the actual publication order: the encoder validates every input
	// field while producing the plain image (encodeIndex -> validRecordContext
	// -> ErrInvalidIndex) and Build never creates any state for a rejected
	// input; for a valid input, the only read-side guarantees on the
	// just-produced payload are its digest (matching the manifest) and a
	// successful bounded decode, not a second structural validation pass.
	t.Run("invalid record rejected before any state is created", func(t *testing.T) {
		roots, state := storeRoots(t)
		invalid := testRecord(testRecordA, "a.go", strings.Repeat("q", 513), []string{"a"})
		if _, err := Build(roots, testManifest(), []model.Record{invalid}); !errors.Is(err, ErrInvalidIndex) {
			t.Fatalf("Build error = %v, want ErrInvalidIndex", err)
		}
		if _, err := os.Stat(state); !os.IsNotExist(err) {
			t.Fatalf("rejected input created state: %v", err)
		}
	})

	t.Run("valid payload is proven by digest and decode, not by a structural pass", func(t *testing.T) {
		records := buildCancellationRecords(64)
		artifacts, err := prepareGeneration(testManifest(), records)
		if err != nil {
			t.Fatal(err)
		}
		if got := sha256ID(artifacts.payload); got != artifacts.manifestValue.PayloadDigest {
			t.Fatalf("payload digest = %q, want manifest PayloadDigest %q", got, artifacts.manifestValue.PayloadDigest)
		}
		decoded, _, err := decodeIndex(artifacts.payload)
		if err != nil {
			t.Fatalf("decodeIndex(payload) = %v", err)
		}
		if len(decoded) != len(records) {
			t.Fatalf("decoded record count = %d, want %d", len(decoded), len(records))
		}
	})
}

func TestVerifyGenerationArtifactsRejectsBytesThatDifferFromTheArtifacts(t *testing.T) {
	roots, state := storeRoots(t)
	snapshot := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	artifacts, err := prepareGeneration(snapshot.Manifest, snapshot.Records)
	if err != nil {
		t.Fatal(err)
	}
	path := installedPath(state, snapshot.Manifest.GenerationIdentity, indexFilename)
	data := mustRead(t, path)
	data[len(data)-1] ^= 0xff
	writeExisting(t, path, data)
	stateDir, generations := openGenerations(t, roots)
	defer closeDirectories(stateDir, generations)
	if err := verifyGenerationArtifacts(boundaryFilesystem{}, generations, artifacts); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("verify error = %v, want ErrStoreCorrupt", err)
	}
}

func TestTrustedPublicationRejectsCurrentRenameFault(t *testing.T) {
	roots, state := storeRoots(t)
	previous := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	injected := errors.New("trusted rename seam")
	_, err := buildWithFilesystemObservedContextBarrierTrusted(
		context.Background(),
		boundaryFilesystem{faults: Faults{BeforeCurrentRename: injected}},
		roots,
		manifestVariant("b"),
		[]model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})},
		buildHooks{},
		nil,
		&previous,
	)
	if !errors.Is(err, injected) {
		t.Fatalf("trusted rename error = %v, want injected", err)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed after trusted rename fault: before=%q after=%q", currentBefore, currentAfter)
	}
	loaded, loadErr := Load(roots, previous.IndexIdentity)
	if loadErr != nil || loaded.IndexIdentity != previous.IndexIdentity {
		t.Fatalf("previous generation = %#v, %v", loaded, loadErr)
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
	testRecordD = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
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

func TestIndexV3PersistsCanonicalQueryStructuresAndAuthenticatedSourceCatalog(t *testing.T) {
	records := []model.Record{
		testRecord(testRecordB, "docs/guide.md", "Guide Setup", []string{"guide", "setup"}),
		testRecord(testRecordA, "pkg/service.go", "pkg.Service", []string{"alias", "service"}),
	}
	records[0].RecordKind, records[0].SourceType, records[0].Language = model.Heading, "document", "markdown"

	catalog := model.SourceCatalog{
		Paths:              []model.SourcePath{{RelativePath: "docs/guide.md", Language: "markdown", Size: 12, SHA256: strings.Repeat("a", 64)}},
		Exclusions:         []model.SourceExclusion{{RelativePath: "vendor", Reason: "vendored"}},
		ExtractionWarnings: []model.SourceWarning{},
		Warnings:           []string{},
	}
	encoded, err := encodeIndexCatalogObservedStatsContext(context.Background(), records, catalog, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	plain := mustDecompress(t, encoded)
	if indexFormatVersion != 4 {
		t.Fatalf("format constant = %d, want reference-field format 4", indexFormatVersion)
	}
	if version := binary.BigEndian.Uint16(plain[len(indexMagic):]); version != indexFormatVersion {
		t.Fatalf("index version = %d, want authenticated catalog format %d", version, indexFormatVersion)
	}
	var decodedCatalog model.SourceCatalog
	decoded, postings, queryIndex, err := decodeIndexContextWithCatalogObserved(context.Background(), encoded, nil, &decodedCatalog)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(decodedCatalog, catalog) {
		t.Fatalf("source catalog = %#v, want %#v", decodedCatalog, catalog)
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

func TestSourceCatalogRejectsNonCanonicalAndOversizedInputsBeforeMarshal(t *testing.T) {
	validPath := model.SourcePath{RelativePath: "a.go", Language: "go", Size: 1, SHA256: strings.Repeat("a", 64)}
	validExclusion := model.SourceExclusion{RelativePath: "vendor", Reason: "vendored"}
	for name, catalog := range map[string]model.SourceCatalog{
		"unsorted-paths": {
			Paths: []model.SourcePath{
				{RelativePath: "z.go", Language: "go", Size: 1, SHA256: strings.Repeat("a", 64)}, validPath,
			},
		},
		"duplicate-paths": {Paths: []model.SourcePath{validPath, validPath}},
		"unsafe-path":     {Paths: []model.SourcePath{{RelativePath: "../a.go", Language: "go", Size: 1, SHA256: strings.Repeat("a", 64)}}},
		"unsorted-exclusions": {
			Exclusions: []model.SourceExclusion{
				{RelativePath: "z", Reason: "ignored"}, validExclusion,
			},
		},
		"duplicate-exclusions": {Exclusions: []model.SourceExclusion{validExclusion, validExclusion}},
		"unsafe-exclusion":     {Exclusions: []model.SourceExclusion{{RelativePath: "a/../b", Reason: "ignored"}}},
		"warning-without-path": {ExtractionWarnings: []model.SourceWarning{{RelativePath: "a.go", Codes: []string{"python-dynamic-lookup"}}}},
		"duplicate-warning-codes": {
			Paths: []model.SourcePath{validPath}, ExtractionWarnings: []model.SourceWarning{{RelativePath: "a.go", Codes: []string{"python-dynamic-lookup", "python-dynamic-lookup"}}},
		},
		"unsafe-warning-path": {
			Paths: []model.SourcePath{validPath}, ExtractionWarnings: []model.SourceWarning{{RelativePath: "../a.go", Codes: []string{"python-dynamic-lookup"}}},
		},
		"oversized-path": {Paths: []model.SourcePath{{RelativePath: strings.Repeat("a", maximumIndexStringBytes+1), Language: "go", Size: 1, SHA256: strings.Repeat("a", 64)}}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := encodeSourceCatalog(catalog); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("encodeSourceCatalog(%#v) error = %v, want ErrInvalidIndex", catalog, err)
			}
		})
	}

	// The frozen catalog byte limit must reject a hostile count before json.Marshal
	// is allowed to allocate the aggregate JSON buffer.
	oversized := model.SourceCatalog{Paths: make([]model.SourcePath, maximumSourceCatalogEntries)}
	for index := range oversized.Paths {
		oversized.Paths[index] = model.SourcePath{RelativePath: fmt.Sprintf("p/%06d.go", index), Language: "go", Size: 1, SHA256: strings.Repeat("a", 64)}
	}
	if _, err := encodeSourceCatalog(oversized); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("oversized source catalog error = %v, want ErrInvalidIndex", err)
	}
}

func TestSourceCatalogRawPreflightEnforcesEightMiBBeforeMaterialization(t *testing.T) {
	exact := bytes.Repeat([]byte{' '}, maximumSourceCatalogBytes)
	if _, err := preflightSourceCatalogJSON(exact); errors.Is(err, errSourceCatalogRawTooLarge) {
		t.Fatalf("exact %d-byte catalog rejected as raw oversize", len(exact))
	}
	over := append(exact, ' ')
	if _, err := preflightSourceCatalogJSON(over); !errors.Is(err, errSourceCatalogRawTooLarge) {
		t.Fatalf("%d-byte catalog error = %v, want raw-size refusal", len(over), err)
	}

	// This raw count is under 8 MiB but would materialize more than the frozen
	// entry ceiling if the decoder built []SourcePath before token preflight.
	var hugeArray bytes.Buffer
	hugeArray.WriteString(`{"Paths":[`)
	for index := 0; index <= maximumSourceCatalogEntries; index++ {
		if index != 0 {
			hugeArray.WriteByte(',')
		}
		hugeArray.WriteString(`{}`)
	}
	hugeArray.WriteString(`],"Exclusions":[],"Partial":false,"Warnings":[]}`)
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	if _, err := preflightSourceCatalogJSON(hugeArray.Bytes()); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("huge-array preflight error = %v, want ErrInvalidIndex", err)
	}
	runtime.ReadMemStats(&after)
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 2<<20 {
		t.Fatalf("huge-array preflight allocated %d bytes", allocated)
	}

	hugeString := []byte(`{"Paths":[{"RelativePath":"` + strings.Repeat("a", maximumIndexStringBytes+1) + `","Language":"go","Size":1,"SHA256":"` + strings.Repeat("a", 64) + `"}],"Exclusions":[],"Partial":false,"Warnings":[]}`)
	if _, err := preflightSourceCatalogJSON(hugeString); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("huge-string preflight error = %v, want ErrInvalidIndex", err)
	}
	tooManyWarnings := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[],"Partial":false,"Warnings":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}`)
	if _, err := preflightSourceCatalogJSON(tooManyWarnings); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("warning-count preflight error = %v, want ErrInvalidIndex", err)
	}
}

func TestSourceCatalogRawPreflightRejectsNonCanonicalFieldAliases(t *testing.T) {
	tooManyWarnings := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[],"Partial":false,"W\u0061rnings":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}`)
	if _, err := preflightSourceCatalogJSON(tooManyWarnings); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("escaped Warnings count error = %v, want ErrInvalidIndex", err)
	}
	caseFoldedWarnings := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[],"Partial":false,"w\u0061rnings":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}`)
	if _, err := preflightSourceCatalogJSON(caseFoldedWarnings); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("case-folded escaped Warnings count error = %v, want ErrInvalidIndex", err)
	}
	tooManyCodes := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[{"RelativePath":"a.go","C\u006fdes":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}],"Partial":false,"Warnings":[]}`)
	if _, err := preflightSourceCatalogJSON(tooManyCodes); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("escaped Codes count error = %v, want ErrInvalidIndex", err)
	}
	simpleFoldWarnings := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[],"Partial":false,"Warning\u017f":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}`)
	if _, err := preflightSourceCatalogJSON(simpleFoldWarnings); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("SimpleFold Warnings alias error = %v, want ErrInvalidIndex", err)
	}
	simpleFoldCodes := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[{"RelativePath":"a.go","Code\u017f":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems) + `]}],"Partial":false,"Warnings":[]}`)
	if _, err := preflightSourceCatalogJSON(simpleFoldCodes); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("SimpleFold Codes alias error = %v, want ErrInvalidIndex", err)
	}
	firstHalf := maximumSourceCatalogEntries / 2
	escapedCombinedEntries := []byte(`{"P\u0061ths":[null` + strings.Repeat(`,null`, firstHalf-1) + `],"Excl\u0075sions":[null` + strings.Repeat(`,null`, maximumSourceCatalogEntries-firstHalf) + `]}`)
	if _, err := preflightSourceCatalogJSON(escapedCombinedEntries); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("escaped combined entry count error = %v, want ErrInvalidIndex", err)
	}

	tests := []struct {
		name      string
		alias     string
		canonical string
		value     string
		maximum   int
	}{
		{name: "relative path", alias: `Rel\u0061tivePath`, canonical: "RelativePath", value: "a", maximum: maximumIndexStringBytes},
		{name: "language", alias: `L\u0061nguage`, canonical: "Language", value: "a", maximum: 128},
		{name: "sha256", alias: `SH\u0041256`, canonical: "SHA256", value: "a", maximum: 64},
		{name: "reason", alias: `Re\u0061son`, canonical: "Reason", value: "a", maximum: 128},
		{name: "warning", alias: `W\u0061rnings`, canonical: "Warnings", value: "a", maximum: 128},
		{name: "code", alias: `C\u006fdes`, canonical: "Codes", value: "a", maximum: 128},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var alias, exact, over []byte
			switch test.name {
			case "warning", "code":
				alias = []byte(`{"` + test.alias + `":["a"]}`)
				exact = []byte(`{"` + test.canonical + `":["` + strings.Repeat(test.value, test.maximum) + `"]}`)
				over = []byte(`{"` + test.canonical + `":["` + strings.Repeat(test.value, test.maximum+1) + `"]}`)
			default:
				alias = []byte(`{"` + test.alias + `":"a"}`)
				exact = []byte(`{"` + test.canonical + `":"` + strings.Repeat(test.value, test.maximum) + `"}`)
				over = []byte(`{"` + test.canonical + `":"` + strings.Repeat(test.value, test.maximum+1) + `"}`)
			}
			if _, err := preflightSourceCatalogJSON(alias); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("noncanonical alias error = %v, want ErrInvalidIndex", err)
			}
			if _, err := preflightSourceCatalogJSON(exact); err != nil {
				t.Fatalf("exact canonical boundary error = %v", err)
			}
			if _, err := preflightSourceCatalogJSON(over); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("canonical maximum+1 error = %v, want ErrInvalidIndex", err)
			}
		})
	}
	for _, field := range []string{"Warnings", "Codes"} {
		exact := []byte(`{"` + field + `":["a"` + strings.Repeat(`,"a"`, policy.ProductionLimits().MaximumCollectionItems-1) + `]}`)
		if _, err := preflightSourceCatalogJSON(exact); err != nil {
			t.Fatalf("exact canonical %s count error = %v", field, err)
		}
	}
	hostileEscapedString := []byte(`{"L\u0061nguage":"` + strings.Repeat("a", 1<<20) + `"}`)
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	if _, err := preflightSourceCatalogJSON(hostileEscapedString); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("hostile escaped string error = %v, want ErrInvalidIndex", err)
	}
	runtime.ReadMemStats(&after)
	if allocated := after.TotalAlloc - before.TotalAlloc; allocated > 1<<20 {
		t.Fatalf("hostile escaped string preflight allocated %d bytes", allocated)
	}
}

func TestSourceCatalogDecodeChargesRawCapacityAgainstCurrentLiveBudget(t *testing.T) {
	catalog := model.SourceCatalog{
		Paths:              []model.SourcePath{},
		Exclusions:         []model.SourceExclusion{},
		ExtractionWarnings: []model.SourceWarning{},
		Warnings:           []string{},
	}
	encoded, err := encodeSourceCatalog(catalog)
	if err != nil {
		t.Fatal(err)
	}
	preflight, err := preflightSourceCatalogJSON(encoded)
	if err != nil {
		t.Fatal(err)
	}
	backing := make([]byte, len(encoded), len(encoded)+(2<<20))
	copy(backing, encoded)
	// Raw capacity, decoder token storage, canonical re-encode bytes, and the
	// decoded object graph are all live at canonicality comparison.
	catalogLive := int64(cap(backing)+2*len(backing)) + preflight.decodedBytes
	budget := decodeMemoryBudget{used: maximumIndexPeakBytes - catalogLive}
	if _, err := decodeSourceCatalogBudgeted(backing, &budget); err != nil {
		t.Fatalf("exact cumulative live budget error = %v", err)
	}
	budget = decodeMemoryBudget{used: maximumIndexPeakBytes - catalogLive + 1}
	if _, err := decodeSourceCatalogBudgeted(backing, &budget); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("cumulative live budget maximum+1 error = %v, want ErrInvalidIndex", err)
	}
}

func TestIntegratedIndexCatalogBudgetCountsAliasedRawBytesOnce(t *testing.T) {
	encoded, err := encodeIndex(nil)
	if err != nil {
		t.Fatal(err)
	}
	plain := mustDecompress(t, encoded)
	emptyCatalog := []byte(`{"Paths":[],"Exclusions":[],"ExtractionWarnings":[],"Partial":false,"Warnings":[]}`)
	if !bytes.HasSuffix(plain, emptyCatalog) {
		t.Fatalf("empty index does not end in canonical empty catalog: %q", plain)
	}
	// Hand-derived preflight object bytes: one 64-byte root object, five
	// key strings (decoded lengths plus 16-byte overhead), and four 24-byte
	// arrays. The raw catalog is a slice of plain and owns no second backing.
	const emptyCatalogObjectBytes = int64(64 + (5 + 16) + (10 + 16) + (18 + 16) + (7 + 16) + (8 + 16) + 4*24)
	additionalCatalogBytes := 2*int64(len(emptyCatalog)) + emptyCatalogObjectBytes
	liveBeforeCatalog := int64(cap(encoded)) + int64(len(plain)) + conservativeZlibWorkspaceBytes
	exactAdditionalLive := maximumIndexPeakBytes - liveBeforeCatalog - additionalCatalogBytes
	if exactAdditionalLive < 0 {
		t.Fatal("empty integrated decode already exceeds the peak budget")
	}
	var catalog model.SourceCatalog
	if _, _, _, err := decodeIndexContextWithCatalogLiveBytes(context.Background(), encoded, exactAdditionalLive, &catalog); err != nil {
		t.Fatalf("integrated exact live budget decode error = %v", err)
	}
	if !reflect.DeepEqual(catalog, model.SourceCatalog{Paths: []model.SourcePath{}, Exclusions: []model.SourceExclusion{}, ExtractionWarnings: []model.SourceWarning{}, Warnings: []string{}}) {
		t.Fatalf("integrated catalog = %#v", catalog)
	}
	if _, _, _, err := decodeIndexContextWithCatalogLiveBytes(context.Background(), encoded, exactAdditionalLive+1, nil); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("integrated live budget maximum+1 decode error = %v, want ErrInvalidIndex", err)
	}
	if _, _, err := validateIndexContextWithLiveBytes(context.Background(), encoded, exactAdditionalLive); err != nil {
		t.Fatalf("integrated exact live budget raw validation error = %v", err)
	}
	if _, _, err := validateIndexContextWithLiveBytes(context.Background(), encoded, exactAdditionalLive+1); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("integrated live budget maximum+1 raw validation error = %v, want ErrInvalidIndex", err)
	}
}

func TestSourceCatalogDecodeReservesRawDecodedAndCanonicalBuffers(t *testing.T) {
	catalog := model.SourceCatalog{
		Paths:              []model.SourcePath{{RelativePath: "a.go", Language: "go", Size: 3, SHA256: strings.Repeat("a", 64)}},
		Exclusions:         []model.SourceExclusion{},
		ExtractionWarnings: []model.SourceWarning{},
		Warnings:           []string{"python-dynamic-lookup"},
	}
	encoded, err := encodeSourceCatalog(catalog)
	if err != nil {
		t.Fatal(err)
	}
	preflight, err := preflightSourceCatalogJSON(encoded)
	if err != nil {
		t.Fatal(err)
	}
	budget := decodeMemoryBudget{}
	decoded, err := decodeSourceCatalogBudgeted(encoded, &budget)
	if err != nil {
		t.Fatal(err)
	}
	minimum := int64(len(encoded))*2 + preflight.decodedBytes
	if !reflect.DeepEqual(decoded, catalog) || budget.used < minimum {
		t.Fatalf("decoded=%#v reservation=%d, want at least raw+canonical+decoded %d", decoded, budget.used, minimum)
	}
}

func TestIndexCatalogAdvertisedNinetySixMiBIsRejectedAtEightMiBBoundary(t *testing.T) {
	encoded, err := encodeIndex(nil)
	if err != nil {
		t.Fatal(err)
	}
	plain := mustDecompress(t, encoded)
	emptyCatalog, err := encodeSourceCatalog(model.SourceCatalog{})
	if err != nil {
		t.Fatal(err)
	}
	lengthOffset := len(plain) - len(emptyCatalog) - 4
	if lengthOffset < 0 || binary.BigEndian.Uint32(plain[lengthOffset:lengthOffset+4]) != uint32(len(emptyCatalog)) {
		t.Fatal("could not locate authenticated catalog length")
	}
	binary.BigEndian.PutUint32(plain[lengthOffset:lengthOffset+4], uint32(maximumDecompressedIndexBytes))
	encoded = mustCompress(plain[:lengthOffset+4])
	if _, _, _, err := decodeIndexContext(context.Background(), encoded); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("96MiB advertised decode error = %v", err)
	}
	if _, _, err := validateIndex(encoded); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("96MiB advertised validation error = %v", err)
	}
}

func TestIndexV3RejectsPreCatalogV2Payload(t *testing.T) {
	encoded, err := encodeIndex([]model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if err != nil {
		t.Fatal(err)
	}
	legacy := mutateHeader(encoded, func(header []byte) { binary.BigEndian.PutUint16(header[len(indexMagic):], 2) })
	if _, _, _, err := decodeIndexContext(context.Background(), legacy); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("pre-catalog v2 decode error = %v, want ErrInvalidIndex", err)
	}
	if _, _, err := validateIndex(legacy); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("pre-catalog v2 validation error = %v, want ErrInvalidIndex", err)
	}
}

// TestFormatRoundTripsReferenceFields freezes the two record fields the
// format-4 tuple adds after the preview: a reference record carries the target
// table of the definition it belongs to and the number of merged occurrences,
// an import record carries only the imported module specifier, and every other
// kind carries neither. The raw validator must accept the same bytes the
// decoder returns.
func TestFormatRoundTripsReferenceFields(t *testing.T) {
	definition := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"run"})
	reference := testRecord(testRecordB, "pkg/a.py", "a.run", []string{"helpers.load", "osp.join"})
	reference.RecordKind = model.Reference
	reference.TargetName, reference.ReferenceCount = "helpers.load:5:2;osp.join:7:1", 3
	imported := testRecord(testRecordD, "pkg/a.py", "helpers", []string{"helpers"})
	imported.RecordKind = model.Import
	imported.TargetName = "pkg.helpers"
	// An import record may still omit the target: the extractors start writing
	// the module specifier in the follow-up task, and indexes built before that
	// must stay encodable.
	untargetedImport := testRecord(testRecordB[:len(testRecordB)-1]+"a", "pkg/a.py", "os", []string{"os"})
	untargetedImport.RecordKind = model.Import
	encoded, err := encodeIndex([]model.Record{definition, reference, imported, untargetedImport})
	if err != nil {
		t.Fatal(err)
	}
	if recordCount, _, err := validateIndex(encoded); err != nil || recordCount != 4 {
		t.Fatalf("validateIndex = %d, %v", recordCount, err)
	}
	decoded, _, err := decodeIndex(encoded)
	if err != nil {
		t.Fatal(err)
	}
	byIdentity := map[string]model.Record{}
	for _, record := range decoded {
		byIdentity[record.Identity] = record
	}
	if got := byIdentity[testRecordB]; got.RecordKind != model.Reference || got.TargetName != "helpers.load:5:2;osp.join:7:1" || got.ReferenceCount != 3 {
		t.Fatalf("reference round trip = %#v", got)
	}
	if got := byIdentity[testRecordD]; got.TargetName != "pkg.helpers" || got.ReferenceCount != 0 {
		t.Fatalf("import round trip = %#v", got)
	}
	if got := byIdentity[testRecordA]; got.TargetName != "" || got.ReferenceCount != 0 {
		t.Fatalf("definition round trip = %#v", got)
	}
}

// TestFormatRejectsInconsistentReferenceFields keeps the two new fields tied
// to the record kind: only reference records count occurrences, only reference
// and import records name a target, a reference target is a well-formed table,
// and the record's count is the total the table declares.
func TestFormatRejectsInconsistentReferenceFields(t *testing.T) {
	cases := map[string]func(*model.Record){
		"reference without count":  func(record *model.Record) { record.RecordKind = model.Reference; record.TargetName = "x:1:1" },
		"reference without target": func(record *model.Record) { record.RecordKind = model.Reference; record.ReferenceCount = 1 },
		"definition with count":    func(record *model.Record) { record.ReferenceCount = 1 },
		"definition with target":   func(record *model.Record) { record.TargetName = "x" },
		"negative count": func(record *model.Record) {
			record.RecordKind = model.Reference
			record.TargetName = "x:1:1"
			record.ReferenceCount = -1
		},
		"target that is a plain name": func(record *model.Record) {
			record.RecordKind = model.Reference
			record.ReferenceCount = 1
			record.TargetName = "helpers.load"
		},
		"count that is not the table total": func(record *model.Record) {
			record.RecordKind = model.Reference
			record.ReferenceCount = 2
			record.TargetName = "helpers.load:5:2;osp.join:7:1"
		},
		"table beyond the entry bound": func(record *model.Record) {
			entries := make([]model.ReferenceEntry, 0, model.MaximumReferenceTableEntries+1)
			for index := range model.MaximumReferenceTableEntries + 1 {
				entries = append(entries, model.ReferenceEntry{Name: "target" + strconv.Itoa(index), Line: index + 1, Count: 1})
			}
			record.RecordKind = model.Reference
			record.ReferenceCount = len(entries)
			record.TargetName = model.FormatReferenceTable(entries)
		},
		"table beyond the byte bound": func(record *model.Record) {
			record.RecordKind = model.Reference
			record.ReferenceCount = 1
			record.TargetName = strings.Repeat("x", model.MaximumReferenceTableBytes) + ":1:1"
		},
		"target with control character": func(record *model.Record) {
			record.RecordKind = model.Reference
			record.ReferenceCount = 1
			record.TargetName = "load\n:1:1"
		},
		"import specifier beyond the specifier bound": func(record *model.Record) {
			record.RecordKind = model.Import
			record.TargetName = strings.Repeat("x", maximumTargetNameBytes+1)
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			record := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"run"})
			mutate(&record)
			if _, err := encodeIndex([]model.Record{record}); !errors.Is(err, ErrInvalidIndex) {
				t.Fatalf("err = %v, want ErrInvalidIndex", err)
			}
		})
	}
}

// TestFormatRejectsAReferenceCountBeyondTheEncodedWidth guards the uint32 the
// tuple encodes: a wider count must be refused rather than silently truncated.
func TestFormatRejectsAReferenceCountBeyondTheEncodedWidth(t *testing.T) {
	oversized := uint64(math.MaxUint32) + 1
	if oversized > uint64(math.MaxInt) {
		t.Skip("int is narrower than the encoded width on this platform")
	}
	record := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"run"})
	record.RecordKind, record.TargetName, record.ReferenceCount = model.Reference, "helpers.load:5:2", int(oversized)
	if validReferenceFields(record) {
		t.Fatal("a reference count beyond the encoded uint32 was accepted")
	}
}

// TestFormatAcceptsAReferenceTableBeyondTheSpecifierBound proves the target
// field is bounded by the table maximum rather than by the import specifier
// maximum: a full table of long names is larger than any module specifier the
// format accepts.
func TestFormatAcceptsAReferenceTableBeyondTheSpecifierBound(t *testing.T) {
	entries := make([]model.ReferenceEntry, 0, 8)
	for index := range 8 {
		entries = append(entries, model.ReferenceEntry{Name: strings.Repeat("t", 100) + strconv.Itoa(index), Line: index + 1, Count: 1})
	}
	table := model.FormatReferenceTable(entries)
	if len(table) <= maximumTargetNameBytes || len(table) > model.MaximumReferenceTableBytes {
		t.Fatalf("table length = %d, want between %d and %d", len(table), maximumTargetNameBytes, model.MaximumReferenceTableBytes)
	}
	record := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"run"})
	record.RecordKind = model.Reference
	record.TargetName, record.ReferenceCount = table, len(entries)
	encoded, err := encodeIndex([]model.Record{record})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := validateIndex(encoded); err != nil {
		t.Fatalf("validateIndex = %v", err)
	}
	decoded, _, err := decodeIndex(encoded)
	if err != nil || len(decoded) != 1 || decoded[0].TargetName != table {
		t.Fatalf("decoded = %#v, err = %v", decoded, err)
	}
}

// TestValidateIndexDerivesTheSameMapGroupsAsTheEncoder keeps the raw
// validator's group derivation identical to the encoder's now that references
// are excluded from the repository map: a path whose records are all
// references contributes no group, and a reference never represents a path
// even when the structural record there carries weaker evidence.
func TestValidateIndexDerivesTheSameMapGroupsAsTheEncoder(t *testing.T) {
	referenceOnly := testRecord(testRecordA, "pkg/calls.py", "calls", []string{"calls"})
	referenceOnly.RecordKind = model.Reference
	referenceOnly.TargetName, referenceOnly.ReferenceCount = "helpers.load:5:2", 2
	inferred := testRecord(testRecordB, "src/macros.rs", "macros.trace", []string{"trace"})
	inferred.EvidenceClass = model.Inferred
	verifiedReference := testRecord(testRecordD, "src/macros.rs", "macros", []string{"println"})
	verifiedReference.RecordKind = model.Reference
	verifiedReference.TargetName, verifiedReference.ReferenceCount = "println:3:1", 1
	records := []model.Record{referenceOnly, inferred, verifiedReference}
	encoded, err := encodeIndex(records)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := validateIndex(encoded); err != nil {
		t.Fatalf("validateIndex = %v", err)
	}
	_, _, queryIndex, err := decodeIndexContext(context.Background(), encoded)
	if err != nil {
		t.Fatal(err)
	}
	groups, _ := queryIndex.MapGroups()
	if len(groups) != 1 || groups[0].Path != "src/macros.rs" {
		t.Fatalf("map groups = %#v, want only the path with a structural record", groups)
	}
	decoded, _, err := decodeIndex(encoded)
	if err != nil {
		t.Fatal(err)
	}
	if len(groups[0].Ordinals) != 1 || decoded[groups[0].Ordinals[0]].Identity != inferred.Identity {
		t.Fatalf("representative = %#v, want the definition record", groups[0].Ordinals)
	}
}

// TestValidateIndexRejectsCorruptedReferenceCount exercises the raw validator
// on the encoded bytes: zeroing the occurrence count of a reference record
// leaves every length intact, so only the kind/count consistency rule can
// catch it.
func TestValidateIndexRejectsCorruptedReferenceCount(t *testing.T) {
	reference := testRecord(testRecordA, "pkg/a.py", "a.run", []string{"helpers.load"})
	reference.RecordKind = model.Reference
	reference.TargetName, reference.ReferenceCount = "helpers.load:5:2", 2
	encoded, err := encodeIndex([]model.Record{reference})
	if err != nil {
		t.Fatal(err)
	}
	plain := mustDecompress(t, encoded)
	decoder := rawBinaryDecoder{value: plain}
	if _, err := decoder.readBytes(len(indexMagic)); err != nil {
		t.Fatal(err)
	}
	if _, err := decoder.readUint16(); err != nil {
		t.Fatal(err)
	}
	if _, err := decoder.readCount(maximumIndexRecords); err != nil {
		t.Fatal(err)
	}
	if err := skipRawRecord(&decoder); err != nil {
		t.Fatal(err)
	}
	binary.BigEndian.PutUint32(plain[decoder.offset-4:decoder.offset], 0)
	corrupt := mustCompress(plain)
	if _, _, err := validateIndex(corrupt); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("raw validation error = %v, want ErrInvalidIndex", err)
	}
	if _, _, err := decodeIndex(corrupt); !errors.Is(err, ErrInvalidIndex) {
		t.Fatalf("decode error = %v, want ErrInvalidIndex", err)
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

// TestRawRecordMetadataBudgetMatchesTheStructure keeps the raw validator's
// per-record reservation honest: it charges exactly what one metadata entry
// costs, so a record tuple that grows cannot silently exceed the peak budget.
func TestRawRecordMetadataBudgetMatchesTheStructure(t *testing.T) {
	if got := int(unsafe.Sizeof(rawRecordMetadata{})); got != rawRecordMetadataBytes {
		t.Fatalf("rawRecordMetadata = %d bytes, budget charges %d", got, rawRecordMetadataBytes)
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
	// Format 4 adds an empty target name (four length bytes) and the four-byte
	// reference count to every record tuple: 300000 * 8 bytes over format 3.
	if preflight.plainSize != 88_200_322 {
		t.Fatalf("reviewer-shape v4 preflight bytes = %d, want 88200322", preflight.plainSize)
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

func TestPeekMatchesInspectForAValidGeneration(t *testing.T) {
	roots, _ := storeRoots(t)
	built := mustBuild(t, roots, testManifest(), buildCancellationRecords(64))
	inspected, err := InspectContext(context.Background(), roots)
	if err != nil {
		t.Fatal(err)
	}
	peeked, err := PeekContext(context.Background(), roots)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(peeked, inspected) {
		t.Fatalf("peek differs from inspect:\npeek    %#v\ninspect %#v", peeked, inspected)
	}
	if !peeked.Ready || peeked.IndexIdentity != built.IndexIdentity || peeked.GenerationIdentity != built.Manifest.GenerationIdentity || peeked.InstalledBytes != built.InstalledBytes {
		t.Fatalf("peek = %#v", peeked)
	}
}

func TestPeekPropagatesCancellationAndNilRoots(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	roots, _ := storeRoots(t)
	mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	if _, err := PeekContext(ctx, roots); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled peek error = %v, want context.Canceled", err)
	}
	if _, err := PeekContext(context.Background(), nil); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("nil roots peek error = %v, want ErrStoreCorrupt", err)
	}
}

// resignGenerationWithPayload replaces the installed payload and re-signs the
// manifest, READY marker, generation directory, and CURRENT so every digest and
// identity agrees with the new bytes. It models a writer that can rewrite the
// whole generation, which no digest check can detect; only structural
// validation or decoding can.
func resignGenerationWithPayload(t *testing.T, state string, snapshot Snapshot, payload []byte) string {
	t.Helper()
	manifest := cloneManifest(snapshot.Manifest)
	manifest.PayloadDigest = digestIdentity(payload)
	manifest.IndexIdentity = manifest.PayloadDigest
	identity, err := computeGenerationIdentity(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.GenerationIdentity = identity
	encoded, err := encodeManifest(manifest)
	if err != nil {
		t.Fatal(err)
	}
	oldPath := generationPath(state, snapshot.Manifest.GenerationIdentity)
	newPath := generationPath(state, identity)
	if err := os.Rename(oldPath, newPath); err != nil {
		t.Fatal(err)
	}
	writeExisting(t, filepath.Join(newPath, indexFilename), payload)
	writeExisting(t, filepath.Join(newPath, manifestFilename), encoded)
	writeExisting(t, filepath.Join(newPath, readyFilename), []byte(identity+"\n"))
	writeExisting(t, filepath.Join(state, currentFilename), []byte(generationToken(identity)+"\n"))
	return manifest.IndexIdentity
}

func TestPeekAcceptsDigestConsistentStructuralCorruptionThatInspectAndLoadReject(t *testing.T) {
	// This documents the amended invariant: Peek trusts the digest and the
	// generation identity; a structurally corrupt payload that was re-signed is
	// caught by Inspect (metrics) and by Load's decoder (every query), never
	// surfaced as records.
	roots, state := storeRoots(t)
	snapshot := mustBuild(t, roots, testManifest(), buildCancellationRecords(8))
	corrupt := mutateFirstQueryOrdinal(t, installedFile(t, state, snapshot.Manifest.GenerationIdentity, indexFilename), 99)
	resigned := resignGenerationWithPayload(t, state, snapshot, corrupt)

	peeked, err := PeekContext(context.Background(), roots)
	if err != nil || !peeked.Ready || peeked.IndexIdentity != resigned {
		t.Fatalf("peek = %#v, %v; want ready with the re-signed identity", peeked, err)
	}
	if _, err := InspectContext(context.Background(), roots); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("Inspect error = %v, want ErrStoreCorrupt", err)
	}
	if _, err := LoadContext(context.Background(), roots, resigned); !errors.Is(err, ErrStoreCorrupt) {
		t.Fatalf("Load error = %v, want ErrStoreCorrupt", err)
	}
}

// TestBenchPeekInspectLoad prints the read-path timings the Phase 2 spec
// reasons about, plus prepareGenerationContext ("prepare": deterministic
// in-memory preparation) and BuildContext ("publish": prepare plus state
// I/O), so the fixed cost of preparation can be told apart from publication.
// It is opt-in: TAF_STORE_BENCH=1 go test -run Bench -v ./internal/store
// TAF_STORE_BENCH_RECORDS overrides the record count (default 6000, ignored
// when TAF_BENCH_STATE names a real state root to measure instead).
func TestBenchPeekInspectLoad(t *testing.T) {
	if os.Getenv("TAF_STORE_BENCH") != "1" {
		t.Skip("set TAF_STORE_BENCH=1 to print Peek, Inspect, and Load timings")
	}
	count := 6000
	if raw := os.Getenv("TAF_STORE_BENCH_RECORDS"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed <= 0 {
			t.Fatalf("TAF_STORE_BENCH_RECORDS=%q is not a positive integer", raw)
		}
		count = parsed
	}
	ctx := context.Background()
	var roots *boundary.Roots
	var snapshot Snapshot
	if benchState := os.Getenv("TAF_BENCH_STATE"); benchState != "" {
		// Measure a real, already-built state root instead of a synthetic one.
		// The repository root only needs to satisfy boundary validation; Build
		// and the read paths never inspect its contents.
		repository := filepath.Join(t.TempDir(), "repository")
		if err := os.MkdirAll(filepath.Join(repository, ".git"), 0o700); err != nil {
			t.Fatal(err)
		}
		validated, err := boundary.ValidateRoots(wire.Envelope{RepositoryRoot: repository, StateRoot: benchState})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = validated.Close() })
		roots = &validated
		status, err := PeekContext(ctx, roots)
		if err != nil {
			t.Fatal(err)
		}
		loaded, err := LoadContext(ctx, roots, status.IndexIdentity)
		if err != nil {
			t.Fatal(err)
		}
		snapshot = loaded
		t.Logf("records=%d installed_bytes=%d", len(snapshot.Records), snapshot.InstalledBytes)
	} else {
		roots, _ = storeRoots(t)
		start := time.Now()
		snapshot = mustBuild(t, roots, testManifest(), buildCancellationRecords(count))
		t.Logf("records=%d build=%s installed_bytes=%d", count, time.Since(start), snapshot.InstalledBytes)
	}
	stages := []struct {
		name string
		run  func() error
	}{
		{"peek", func() error { _, err := PeekContext(ctx, roots); return err }},
		{"inspect", func() error { _, err := InspectContext(ctx, roots); return err }},
		{"load", func() error { _, err := LoadContext(ctx, roots, snapshot.IndexIdentity); return err }},
		{"prepare", func() error {
			_, err := prepareGenerationContext(ctx, snapshot.Manifest, snapshot.Records, nil)
			return err
		}},
		{"publish", func() error {
			publishRoots, _ := storeRoots(t)
			_, err := BuildContext(ctx, publishRoots, snapshot.Manifest, snapshot.Records)
			return err
		}},
	}
	for _, stage := range stages {
		durations := make([]time.Duration, 0, 10)
		for range 10 {
			began := time.Now()
			if err := stage.run(); err != nil {
				t.Fatal(err)
			}
			durations = append(durations, time.Since(began))
		}
		slices.Sort(durations)
		t.Logf("%-7s median=%s min=%s max=%s", stage.name, durations[5], durations[0], durations[9])
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
			makeInsecurePermissions(t, filepath.Join(state, currentFilename), 0o644)
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
			makeInsecurePermissions(t, generationPath(state, snapshot.Manifest.GenerationIdentity), 0o755)
		}},
		{name: "insecure manifest mode", mutate: func(t *testing.T, state string, snapshot Snapshot) {
			makeInsecurePermissions(t, installedPath(state, snapshot.Manifest.GenerationIdentity, manifestFilename), 0o644)
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
			if _, err := PeekContext(context.Background(), roots); !errors.Is(err, ErrStoreCorrupt) {
				t.Fatalf("Peek error = %v, want ErrStoreCorrupt", err)
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
	if _, err := PeekContext(context.Background(), roots); !errors.Is(err, ErrNoCurrent) {
		t.Fatalf("Peek missing CURRENT error = %v, want ErrNoCurrent", err)
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

func TestCurrentGenerationReadsOnlyTheSelectedPointerIdentity(t *testing.T) {
	roots, _ := storeRoots(t)
	if _, err := CurrentGenerationContext(context.Background(), roots); !errors.Is(err, ErrNoCurrent) {
		t.Fatalf("missing current = %v, want ErrNoCurrent", err)
	}
	snapshot := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	identity, err := CurrentGenerationContext(context.Background(), roots)
	if err != nil || identity != snapshot.Manifest.GenerationIdentity {
		t.Fatalf("current generation = %q, %v; want %q", identity, err, snapshot.Manifest.GenerationIdentity)
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

func TestBuildBarrierRunsAfterCurrentPointerTempIsPrepared(t *testing.T) {
	roots, state := storeRoots(t)
	first := mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	injected := errors.New("barrier rejected publication")
	sawPreparedPointer := false
	_, err := BuildContextWithBarrier(context.Background(), roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}, func() error {
		entries, readErr := os.ReadDir(state)
		if readErr != nil {
			return readErr
		}
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".CURRENT-") {
				sawPreparedPointer = true
			}
		}
		return injected
	})
	if !errors.Is(err, injected) || !sawPreparedPointer {
		t.Fatalf("barrier error=%v prepared-pointer=%v", err, sawPreparedPointer)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed after barrier failure: before=%q after=%q", currentBefore, currentAfter)
	}
	loaded, loadErr := Load(roots, first.IndexIdentity)
	if loadErr != nil || loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("previous generation = %#v, %v", loaded, loadErr)
	}
}

func TestBuildSameGenerationBarrierRunsOnceAtFinalSeam(t *testing.T) {
	roots, state := storeRoots(t)
	records := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	first := mustBuild(t, roots, testManifest(), records)
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	generationBefore := generationBytes(t, state, first.Manifest.GenerationIdentity)
	injected := errors.New("same-generation barrier rejected")
	materialized, barrierCalls := false, 0
	_, err := buildWithFilesystemObservedContextBarrier(
		context.Background(), boundaryFilesystem{}, roots, testManifest(), records,
		buildHooks{materialized: func() { materialized = true }},
		func() error {
			barrierCalls++
			if !materialized {
				t.Fatal("same-generation barrier ran before final materialization")
			}
			return injected
		},
	)
	if !errors.Is(err, injected) || barrierCalls != 1 {
		t.Fatalf("same-generation error = %v, barrier calls = %d", err, barrierCalls)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed after same-generation barrier failure: before=%q after=%q", currentBefore, currentAfter)
	}
	if generationAfter := generationBytes(t, state, first.Manifest.GenerationIdentity); !bytes.Equal(generationBefore, generationAfter) {
		t.Fatal("immutable generation changed after same-generation barrier failure")
	}
}

func TestBuildSameGenerationBarrierPreservesUnchangedSuccess(t *testing.T) {
	roots, state := storeRoots(t)
	records := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	first := mustBuild(t, roots, testManifest(), records)
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	barrierCalls := 0
	repeated, err := BuildContextWithBarrier(context.Background(), roots, testManifest(), records, func() error {
		barrierCalls++
		return nil
	})
	if err != nil || barrierCalls != 1 || !reflect.DeepEqual(repeated, first) {
		t.Fatalf("same-generation success = %#v, error = %v, barrier calls = %d", repeated, err, barrierCalls)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed for same-generation success: before=%q after=%q", currentBefore, currentAfter)
	}
}

func TestBuildSameGenerationBarrierCancellationReturnsError(t *testing.T) {
	roots, state := storeRoots(t)
	records := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	first := mustBuild(t, roots, testManifest(), records)
	currentBefore := mustRead(t, filepath.Join(state, currentFilename))
	ctx, cancel := context.WithCancel(context.Background())
	barrierCalls := 0
	_, err := BuildContextWithBarrier(ctx, roots, testManifest(), records, func() error {
		barrierCalls++
		cancel()
		return nil
	})
	if !errors.Is(err, context.Canceled) || barrierCalls != 1 {
		t.Fatalf("same-generation cancellation error = %v, barrier calls = %d", err, barrierCalls)
	}
	if currentAfter := mustRead(t, filepath.Join(state, currentFilename)); !bytes.Equal(currentBefore, currentAfter) {
		t.Fatalf("CURRENT changed after same-generation cancellation: before=%q after=%q", currentBefore, currentAfter)
	}
	loaded, loadErr := Load(roots, first.IndexIdentity)
	if loadErr != nil || loaded.IndexIdentity != first.IndexIdentity {
		t.Fatalf("unchanged generation = %#v, %v", loaded, loadErr)
	}
}

func TestBuildSameGenerationBarrierSerializesConcurrentValidation(t *testing.T) {
	roots, _ := storeRoots(t)
	records := []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})}
	mustBuild(t, roots, testManifest(), records)
	firstEntered := make(chan struct{})
	secondEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	errorsSeen := make(chan error, 2)
	go func() {
		_, err := BuildContextWithBarrier(context.Background(), roots, testManifest(), records, func() error {
			close(firstEntered)
			<-releaseFirst
			return nil
		})
		errorsSeen <- err
	}()
	select {
	case <-firstEntered:
	case <-time.After(2 * time.Second):
		t.Fatal("first same-generation barrier was not invoked")
	}
	go func() {
		_, err := BuildContextWithBarrier(context.Background(), roots, testManifest(), records, func() error {
			close(secondEntered)
			return nil
		})
		errorsSeen <- err
	}()
	select {
	case <-secondEntered:
		t.Fatal("second same-generation barrier entered while first held publication lock")
	case <-time.After(250 * time.Millisecond):
	}
	close(releaseFirst)
	for range 2 {
		if err := <-errorsSeen; err != nil {
			t.Fatal(err)
		}
	}
	select {
	case <-secondEntered:
	default:
		t.Fatal("second same-generation barrier never ran after lock release")
	}
}

func TestBuildCurrentRenameFaultOccursBeforeAtomicBarrier(t *testing.T) {
	roots, _ := storeRoots(t)
	mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	injected := errors.New("rename seam")
	barrierCalls := 0
	_, err := buildWithFilesystemObservedContextBarrier(
		context.Background(),
		boundaryFilesystem{faults: Faults{BeforeCurrentRename: injected}},
		roots,
		manifestVariant("b"),
		[]model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})},
		buildHooks{},
		func() error { barrierCalls++; return nil },
	)
	if !errors.Is(err, injected) || barrierCalls != 0 {
		t.Fatalf("rename error=%v barrier calls=%d, want fault before barrier", err, barrierCalls)
	}
}

func TestBuildAtomicBarrierSerializesConcurrentCurrentSelection(t *testing.T) {
	roots, _ := storeRoots(t)
	mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	firstEntered := make(chan struct{})
	secondEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	errorsSeen := make(chan error, 2)
	go func() {
		_, err := BuildContextWithBarrier(context.Background(), roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}, func() error {
			close(firstEntered)
			<-releaseFirst
			return nil
		})
		errorsSeen <- err
	}()
	<-firstEntered
	go func() {
		_, err := BuildContextWithBarrier(context.Background(), roots, manifestVariant("c"), []model.Record{testRecord(testRecordA, "c.go", "C", []string{"c"})}, func() error {
			close(secondEntered)
			return nil
		})
		errorsSeen <- err
	}()
	enteredWhileLocked := false
	select {
	case <-secondEntered:
		enteredWhileLocked = true
	case <-time.After(250 * time.Millisecond):
	}
	close(releaseFirst)
	for range 2 {
		if err := <-errorsSeen; err != nil {
			t.Fatal(err)
		}
	}
	if enteredWhileLocked {
		t.Fatal("second publication entered atomic barrier while first held it")
	}
	select {
	case <-secondEntered:
	default:
		t.Fatal("second publication never reached barrier after lock release")
	}
}

func TestConcurrentPublicationRollbackRestoresLatestSelectedCurrent(t *testing.T) {
	roots, state := storeRoots(t)
	mustBuild(t, roots, testManifest(), []model.Record{testRecord(testRecordA, "a.go", "A", []string{"a"})})
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	firstDone := make(chan struct{})
	var first Snapshot
	var firstErr error
	go func() {
		defer close(firstDone)
		first, firstErr = BuildContextWithBarrier(context.Background(), roots, manifestVariant("b"), []model.Record{testRecord(testRecordB, "b.go", "B", []string{"b"})}, func() error {
			close(firstEntered)
			<-releaseFirst
			return nil
		})
	}()
	<-firstEntered
	injected := errors.New("second state sync failed")
	secondDone := make(chan error, 1)
	go func() {
		_, err := BuildWithFaults(roots, manifestVariant("c"), []model.Record{testRecord(testRecordA, "c.go", "C", []string{"c"})}, Faults{BeforeStateSync: injected})
		secondDone <- err
	}()
	deadline := time.Now().Add(2 * time.Second)
	for {
		entries, err := os.ReadDir(state)
		if err != nil {
			t.Fatal(err)
		}
		prepared := 0
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".CURRENT-") {
				prepared++
			}
		}
		if prepared >= 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("second publisher did not prepare CURRENT temp outside the publication lock")
		}
		time.Sleep(time.Millisecond)
	}
	close(releaseFirst)
	<-firstDone
	if firstErr != nil {
		t.Fatal(firstErr)
	}
	if err := <-secondDone; !errors.Is(err, injected) {
		t.Fatalf("second publication error = %v, want injected", err)
	}
	current := mustRead(t, filepath.Join(state, currentFilename))
	if string(current) != tokenFromIdentity(first.Manifest.GenerationIdentity)+"\n" {
		t.Fatalf("rollback CURRENT = %q, want first concurrent selection", current)
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
	if runtime.GOOS == "windows" {
		t.Skip("Windows prevents replacing CURRENT while readers retain an open handle")
	}
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

func (filesystem errorAfterCurrentRenameFilesystem) replaceFileAfterBarrier(directory *boundary.StateDirectory, source, destination string, point faultPoint, barrier func() error) error {
	if err := filesystem.storeFilesystem.replaceFileAfterBarrier(directory, source, destination, point, barrier); err != nil {
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
		FormatVersion: "3", EngineVersion: "engine-v1",
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

func openGenerations(t *testing.T, roots *boundary.Roots) (*boundary.StateDirectory, *boundary.StateDirectory) {
	t.Helper()
	filesystem := boundaryFilesystem{}
	state, err := filesystem.openStateDirectory(roots, "")
	if err != nil {
		t.Fatal(err)
	}
	generations, err := filesystem.openDirectory(state, generationsDirectory)
	if err != nil {
		t.Fatal(err)
	}
	return state, generations
}

func closeDirectories(directories ...*boundary.StateDirectory) {
	filesystem := boundaryFilesystem{}
	for _, directory := range directories {
		_ = filesystem.closeDirectory(directory)
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
