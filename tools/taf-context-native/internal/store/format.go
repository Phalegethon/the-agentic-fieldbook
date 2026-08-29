package store

import (
	"bytes"
	"cmp"
	"compress/zlib"
	"container/heap"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"regexp"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

var (
	ErrInvalidIndex    = errors.New("invalid level1 index")
	ErrInvalidManifest = errors.New("invalid level1 manifest")
	indexMagic         = []byte("TAFL1IDX")
	canonicalName      = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
)

const (
	indexFormatVersion                   uint16 = 2
	maximumDecompressedIndexBytes               = 96 << 20
	maximumEncodedIndexBytes                    = 64 << 20
	maximumManifestBytes                        = 256 << 10
	maximumIndexStringBytes                     = 4096
	maximumIndexRecords                         = 1_000_000
	maximumPostingTerms                         = 1_000_000
	maximumPostingOrdinals                      = 8_000_000
	maximumTermsPerRecord                       = 64
	maximumIndexPeakBytes                       = 512 << 20
	conservativeZlibWorkspaceBytes              = 4 << 20
	conservativeRecordMemoryBytes               = 512
	conservativeCallerTermMemoryBytes           = 16
	conservativeStringAllocationOverhead        = 16
	conservativePostingGroupMemoryBytes         = 160
	contextCheckInterval                        = 1024
	validationByteCheckInterval                 = 256
	exactDecompressionSizingThreshold           = 1 << 20
)

func encodeIndex(input []model.Record) ([]byte, error) {
	return encodeIndexObserved(input, nil)
}

func encodeIndexObserved(input []model.Record, beforeCanonicalPostingSort func()) ([]byte, error) {
	return encodeIndexObservedStats(input, beforeCanonicalPostingSort, nil)
}

func encodeIndexObservedStats(input []model.Record, beforeCanonicalPostingSort func(), postingCount *int) ([]byte, error) {
	return encodeIndexObservedStatsContext(context.Background(), input, beforeCanonicalPostingSort, postingCount, nil)
}

func encodeIndexObservedStatsContext(ctx context.Context, input []model.Record, beforeCanonicalPostingSort func(), postingCount *int, observed func(buildPhase)) ([]byte, error) {
	preflight, err := preflightEncodeIndexContext(ctx, input, observed)
	if err != nil {
		return nil, err
	}
	recordOrder := make([]uint32, len(input))
	for index := range recordOrder {
		recordOrder[index] = uint32(index)
		if (index+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return nil, err
			}
		}
	}
	if err := sortOrdinalsBuildContext(ctx, recordOrder, func(left, right uint32) int {
		return cmp.Compare(input[left].Identity, input[right].Identity)
	}, observed); err != nil {
		return nil, err
	}
	for index := 1; index < len(recordOrder); index++ {
		if index%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseSort); err != nil {
				return nil, err
			}
		}
		if input[recordOrder[index-1]].Identity == input[recordOrder[index]].Identity {
			return nil, ErrInvalidIndex
		}
	}
	postingTerms := make([]string, 0, len(preflight.postings))
	for term := range preflight.postings {
		postingTerms = append(postingTerms, term)
		if len(postingTerms)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return nil, err
			}
		}
	}
	if beforeCanonicalPostingSort != nil {
		beforeCanonicalPostingSort()
	}
	if err := sortStringsBuildContext(ctx, postingTerms, observed); err != nil {
		return nil, err
	}
	nextOffset, err := assignPostingOffsetsContext(ctx, postingTerms, preflight.postings, observed)
	if err != nil {
		return nil, err
	}
	if nextOffset != preflight.totalTerms {
		return nil, ErrInvalidIndex
	}
	ordinals := make([]uint32, preflight.totalTerms)
	termVisits := 0
	for ordinal, inputIndex := range recordOrder {
		for _, term := range input[inputIndex].SearchTerms {
			termVisits++
			if termVisits%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
					return nil, err
				}
			}
			posting := preflight.postings[term]
			if posting.next >= posting.count {
				return nil, ErrInvalidIndex
			}
			ordinals[int(posting.offset+posting.next)] = uint32(ordinal)
			posting.next++
			preflight.postings[term] = posting
		}
	}
	queryTerms := make([]string, 0, len(preflight.queryPostings))
	for term := range preflight.queryPostings {
		queryTerms = append(queryTerms, term)
		if len(queryTerms)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return nil, err
			}
		}
	}
	if err := sortStringsBuildContext(ctx, queryTerms, observed); err != nil {
		return nil, err
	}
	queryOffset, err := assignPostingOffsetsContext(ctx, queryTerms, preflight.queryPostings, observed)
	if err != nil {
		return nil, err
	}
	if queryOffset != preflight.totalQueryTerms {
		return nil, ErrInvalidIndex
	}
	queryOrdinals := make([]uint32, preflight.totalQueryTerms)
	keyVisits := 0
	for ordinal, inputIndex := range recordOrder {
		valid := visitCanonicalQueryKeys(input[inputIndex], func(term string) bool {
			keyVisits++
			if keyVisits%contextCheckInterval == 0 && observeBuildContext(ctx, observed, buildPhaseQueryKeys) != nil {
				return false
			}
			posting := preflight.queryPostings[term]
			if posting.next >= posting.count {
				return false
			}
			queryOrdinals[int(posting.offset+posting.next)] = uint32(ordinal)
			posting.next++
			preflight.queryPostings[term] = posting
			return true
		})
		if !valid {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			return nil, ErrInvalidIndex
		}
	}
	pathOrder := make([]uint32, len(recordOrder))
	for ordinal := range pathOrder {
		pathOrder[ordinal] = uint32(ordinal)
		if (ordinal+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return nil, err
			}
		}
	}
	if err := sortOrdinalsBuildContext(ctx, pathOrder, func(left, right uint32) int {
		return compareIndexedPathOrdinal(input, recordOrder, left, right)
	}, observed); err != nil {
		return nil, err
	}
	mapGroups, mapPartial, err := buildCanonicalMapGroupsContext(ctx, recordsByCanonicalOrder{records: input, order: recordOrder}, pathOrder, policy.ProductionLimits().MaximumLexicalCandidates, func() {
		_ = observeBuildContext(ctx, observed, buildPhaseSort)
	})
	if err != nil {
		return nil, err
	}
	if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
		return nil, err
	}
	var plain bytes.Buffer
	plain.Grow(preflight.plainSize)
	plain.Write(indexMagic)
	writeUint16(&plain, indexFormatVersion)
	writeUint32(&plain, uint32(len(recordOrder)))
	for index, inputIndex := range recordOrder {
		writeCanonicalRecord(&plain, input[inputIndex])
		if (index+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
				return nil, err
			}
		}
	}
	writeUint32(&plain, uint32(len(postingTerms)))
	encodeVisits := 0
	for _, term := range postingTerms {
		posting := preflight.postings[term]
		if posting.next != posting.count {
			return nil, ErrInvalidIndex
		}
		writeString(&plain, term)
		writeUint32(&plain, posting.count)
		start, end := int(posting.offset), int(posting.offset+posting.count)
		for _, ordinal := range ordinals[start:end] {
			writeUint32(&plain, ordinal)
			encodeVisits++
			if encodeVisits%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
					return nil, err
				}
			}
		}
	}
	writeUint32(&plain, uint32(len(queryTerms)))
	rangeEncodeVisits := 0
	for _, term := range queryTerms {
		posting := preflight.queryPostings[term]
		if posting.next != posting.count {
			return nil, ErrInvalidIndex
		}
		writeString(&plain, term)
		writeUint32(&plain, posting.count)
		start, end := int(posting.offset), int(posting.offset+posting.count)
		postingOrdinals := queryOrdinals[start:end]
		rangeCount, err := postingRangeCountContext(ctx, postingOrdinals, observed)
		if err != nil {
			return nil, err
		}
		writeUint32(&plain, uint32(rangeCount))
		for rangeStart := 0; rangeStart < len(postingOrdinals); {
			rangeEnd := rangeStart + 1
			for rangeEnd < len(postingOrdinals) && postingOrdinals[rangeEnd] == postingOrdinals[rangeEnd-1]+1 {
				rangeEnd++
				rangeEncodeVisits++
				if rangeEncodeVisits%contextCheckInterval == 0 {
					if err := observeBuildContext(ctx, observed, buildPhaseRangeEncode); err != nil {
						return nil, err
					}
				}
			}
			writeUint32(&plain, postingOrdinals[rangeStart])
			writeUint32(&plain, uint32(rangeEnd-rangeStart))
			encodeVisits++
			if encodeVisits%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
					return nil, err
				}
			}
			rangeStart = rangeEnd
		}
	}
	writeUint32(&plain, uint32(len(pathOrder)))
	for _, ordinal := range pathOrder {
		writeUint32(&plain, ordinal)
		encodeVisits++
		if encodeVisits%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
				return nil, err
			}
		}
	}
	writeUint32(&plain, uint32(len(mapGroups)))
	for _, group := range mapGroups {
		writeString(&plain, group.Path)
		writeUint32(&plain, uint32(len(group.Ordinals)))
		for _, ordinal := range group.Ordinals {
			writeUint32(&plain, ordinal)
		}
		encodeVisits++
		if encodeVisits%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseEncode); err != nil {
				return nil, err
			}
		}
	}
	if mapPartial {
		writeUint32(&plain, 1)
	} else {
		writeUint32(&plain, 0)
	}
	if plain.Len() > preflight.plainSize || plain.Len() > maximumDecompressedIndexBytes {
		return nil, ErrInvalidIndex
	}

	if err := observeBuildContext(ctx, observed, buildPhaseCompression); err != nil {
		return nil, err
	}
	var encoded bytes.Buffer
	writer, err := zlib.NewWriterLevel(&encoded, zlib.BestCompression)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
	}
	const compressionChunkBytes = 64 << 10
	plainBytes := plain.Bytes()
	for offset := 0; offset < len(plainBytes); offset += compressionChunkBytes {
		end := min(len(plainBytes), offset+compressionChunkBytes)
		if _, err := writer.Write(plainBytes[offset:end]); err != nil {
			_ = writer.Close()
			return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
		}
		if err := observeBuildContext(ctx, observed, buildPhaseCompression); err != nil {
			_ = writer.Close()
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
	}
	if encoded.Len() == 0 || encoded.Len() > maximumEncodedIndexBytes {
		return nil, ErrInvalidIndex
	}
	if postingCount != nil {
		*postingCount = len(postingTerms)
	}
	return encoded.Bytes(), nil
}

type postingMetadata struct {
	count             uint32
	offset            uint32
	next              uint32
	lastRecordPlusOne uint32
}

type encodeIndexPreflight struct {
	plainSize       int
	totalTerms      int
	postings        map[string]postingMetadata
	totalQueryTerms int
	queryPostings   map[string]postingMetadata
}

func sortOrdinalsBuildContext(ctx context.Context, values []uint32, compare func(uint32, uint32) int, observed func(buildPhase)) error {
	if err := observeBuildContext(ctx, observed, buildPhaseSort); err != nil {
		return err
	}
	comparisons := 0
	var canceled error
	slices.SortFunc(values, func(left, right uint32) int {
		comparisons++
		if canceled == nil && comparisons%contextCheckInterval == 0 {
			canceled = observeBuildContext(ctx, observed, buildPhaseSort)
		}
		if canceled != nil {
			return 0
		}
		return compare(left, right)
	})
	if canceled != nil {
		return canceled
	}
	return ctx.Err()
}

func sortStringsBuildContext(ctx context.Context, values []string, observed func(buildPhase)) error {
	if err := observeBuildContext(ctx, observed, buildPhaseSort); err != nil {
		return err
	}
	comparisons := 0
	var canceled error
	slices.SortFunc(values, func(left, right string) int {
		comparisons++
		if canceled == nil && comparisons%contextCheckInterval == 0 {
			canceled = observeBuildContext(ctx, observed, buildPhaseSort)
		}
		if canceled != nil {
			return 0
		}
		return cmp.Compare(left, right)
	})
	if canceled != nil {
		return canceled
	}
	return ctx.Err()
}

func assignPostingOffsets(terms []string, postings map[string]postingMetadata) int {
	nextOffset, _ := assignPostingOffsetsContext(context.Background(), terms, postings, nil)
	return nextOffset
}

func assignPostingOffsetsContext(ctx context.Context, terms []string, postings map[string]postingMetadata, observed func(buildPhase)) (int, error) {
	nextOffset := 0
	for index, term := range terms {
		if (index+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return 0, err
			}
		}
		posting := postings[term]
		posting.offset = uint32(nextOffset)
		nextOffset += int(posting.count)
		postings[term] = posting
	}
	return nextOffset, ctx.Err()
}

func postingRangeCount(ordinals []uint32) int {
	count, _ := postingRangeCountContext(context.Background(), ordinals, nil)
	return count
}

func postingRangeCountContext(ctx context.Context, ordinals []uint32, observed func(buildPhase)) (int, error) {
	count := 0
	visited := 0
	for start := 0; start < len(ordinals); {
		end := start + 1
		visited++
		if visited%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseRangeCount); err != nil {
				return 0, err
			}
		}
		for end < len(ordinals) && ordinals[end] == ordinals[end-1]+1 {
			visited++
			if visited%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhaseRangeCount); err != nil {
					return 0, err
				}
			}
			end++
		}
		count++
		start = end
	}
	return count, ctx.Err()
}

// preflightEncodeIndex rejects inputs that cannot fit the wire or conservative
// process-memory budget before encodeIndex allocates its compact index arrays.
func preflightEncodeIndex(input []model.Record) (encodeIndexPreflight, error) {
	return preflightEncodeIndexContext(context.Background(), input, nil)
}

func preflightEncodeIndexContext(ctx context.Context, input []model.Record, observed func(buildPhase)) (encodeIndexPreflight, error) {
	if err := observeBuildContext(ctx, observed, buildPhasePreflight); err != nil {
		return encodeIndexPreflight{}, err
	}
	if len(input) > maximumIndexRecords {
		return encodeIndexPreflight{}, ErrInvalidIndex
	}
	serialized := int64(len(indexMagic) + 2 + 4 + 4)
	totalTerms := 0
	preflightVisits := 0
	for recordIndex, record := range input {
		if (recordIndex+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhasePreflight); err != nil {
				return encodeIndexPreflight{}, err
			}
		}
		termCount := len(record.SearchTerms)
		if termCount > maximumTermsPerRecord || totalTerms > maximumPostingOrdinals-termCount {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		totalTerms += termCount
		values := [...]string{
			record.Identity, record.Path, record.Language, string(record.RecordKind), record.SourceType,
			record.QualifiedName, record.ExtractionMethod, string(record.EvidenceClass), record.SourceDigest, record.Preview,
		}
		if !addEncodeSize(&serialized, 12+4*int64(termCount), maximumDecompressedIndexBytes) {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		for _, value := range values {
			if !addEncodeSize(&serialized, 4+int64(len(value)), maximumDecompressedIndexBytes) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
		}
		for _, term := range record.SearchTerms {
			preflightVisits++
			if preflightVisits%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhasePreflight); err != nil {
					return encodeIndexPreflight{}, err
				}
			}
			if !addEncodeSize(&serialized, int64(len(term)), maximumDecompressedIndexBytes) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
		}
	}
	recordBytes := serialized
	// The encoded bytes.Buffer may retain growth slack while the complete plain
	// buffer is still live, so charge twice the encoded ceiling.
	peak := int64(maximumDecompressedIndexBytes + 2*maximumEncodedIndexBytes + conservativeZlibWorkspaceBytes)
	for _, amount := range []int64{
		recordBytes,
		int64(len(input)) * conservativeRecordMemoryBytes,
		int64(totalTerms) * (conservativeCallerTermMemoryBytes + 4),
	} {
		if amount < 0 || peak > maximumIndexPeakBytes-amount {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		peak += amount
	}
	postings := make(map[string]postingMetadata)
	queryPostings := make(map[string]postingMetadata)
	paths := &maxStringHeap{}
	heap.Init(paths)
	pathSet := make(map[string]struct{})
	mapMaximum := policy.ProductionLimits().MaximumLexicalCandidates
	if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
		return encodeIndexPreflight{}, err
	}
	keyVisits := 0
	for recordIndex, record := range input {
		if (recordIndex+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return encodeIndexPreflight{}, err
			}
		}
		valid, validationErr := validRecordContext(ctx, record, observed)
		if validationErr != nil {
			return encodeIndexPreflight{}, validationErr
		}
		if !valid {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		if !retainSmallestPath(paths, pathSet, record.Path, mapMaximum) {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		for _, term := range record.SearchTerms {
			keyVisits++
			if keyVisits%contextCheckInterval == 0 {
				if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
					return encodeIndexPreflight{}, err
				}
			}
			valid, validationErr := validTermContext(ctx, term, observed)
			if validationErr != nil {
				return encodeIndexPreflight{}, validationErr
			}
			if !valid {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
			posting, exists := postings[term]
			if exists && posting.lastRecordPlusOne == uint32(recordIndex+1) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
			if !exists {
				if len(postings) == maximumPostingTerms || peak > maximumIndexPeakBytes-conservativePostingGroupMemoryBytes {
					return encodeIndexPreflight{}, ErrInvalidIndex
				}
				peak += conservativePostingGroupMemoryBytes
				if !addEncodeSize(&serialized, 8+int64(len(term)), maximumDecompressedIndexBytes) {
					return encodeIndexPreflight{}, ErrInvalidIndex
				}
			}
			if posting.count == math.MaxUint32 || !addEncodeSize(&serialized, 4, maximumDecompressedIndexBytes) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
			posting.count++
			posting.lastRecordPlusOne = uint32(recordIndex + 1)
			postings[term] = posting
		}
		validKeys := visitCanonicalQueryKeys(record, func(key string) bool {
			keyVisits++
			if keyVisits%contextCheckInterval == 0 && observeBuildContext(ctx, observed, buildPhaseQueryKeys) != nil {
				return false
			}
			if !validQueryKey(key) {
				return false
			}
			posting, exists := queryPostings[key]
			if exists && posting.lastRecordPlusOne == uint32(recordIndex+1) {
				return false
			}
			if !exists {
				if len(queryPostings) == maximumQueryPostingTerms || peak > maximumIndexPeakBytes-conservativePostingGroupMemoryBytes {
					return false
				}
				peak += conservativePostingGroupMemoryBytes
				if !addEncodeSize(&serialized, 12+int64(len(key)), maximumDecompressedIndexBytes) {
					return false
				}
			}
			if posting.count == math.MaxUint32 || !addEncodeSize(&serialized, 8, maximumDecompressedIndexBytes) {
				return false
			}
			posting.count++
			posting.lastRecordPlusOne = uint32(recordIndex + 1)
			queryPostings[key] = posting
			return true
		})
		if !validKeys {
			if err := ctx.Err(); err != nil {
				return encodeIndexPreflight{}, err
			}
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
	}
	totalQueryTerms := 0
	postingIndex := 0
	for _, posting := range queryPostings {
		postingIndex++
		if postingIndex%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhaseQueryKeys); err != nil {
				return encodeIndexPreflight{}, err
			}
		}
		if totalQueryTerms > maximumQueryPostingOrdinals-int(posting.count) {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		totalQueryTerms += int(posting.count)
	}
	if !addEncodeSize(&serialized, 16+int64(len(input))*4, maximumDecompressedIndexBytes) {
		return encodeIndexPreflight{}, ErrInvalidIndex
	}
	for index, path := range *paths {
		if (index+1)%contextCheckInterval == 0 {
			if err := observeBuildContext(ctx, observed, buildPhasePreflight); err != nil {
				return encodeIndexPreflight{}, err
			}
		}
		if !addEncodeSize(&serialized, 12+int64(len(path)), maximumDecompressedIndexBytes) {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
	}
	for _, amount := range []int64{int64(totalQueryTerms) * 4, int64(len(input)) * 4, int64(len(*paths)) * 32} {
		if amount < 0 || peak > maximumIndexPeakBytes-amount {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		peak += amount
	}
	if serialized < 0 || serialized > math.MaxInt {
		return encodeIndexPreflight{}, ErrInvalidIndex
	}
	if err := ctx.Err(); err != nil {
		return encodeIndexPreflight{}, err
	}
	return encodeIndexPreflight{plainSize: int(serialized), totalTerms: totalTerms, postings: postings, totalQueryTerms: totalQueryTerms, queryPostings: queryPostings}, nil
}

func addEncodeSize(total *int64, amount, maximum int64) bool {
	if total == nil || amount < 0 || *total < 0 || amount > maximum-*total {
		return false
	}
	*total += amount
	return true
}

type maxStringHeap []string

func (values maxStringHeap) Len() int           { return len(values) }
func (values maxStringHeap) Less(i, j int) bool { return values[i] > values[j] }
func (values maxStringHeap) Swap(i, j int)      { values[i], values[j] = values[j], values[i] }
func (values *maxStringHeap) Push(value any)    { *values = append(*values, value.(string)) }
func (values *maxStringHeap) Pop() any {
	last := len(*values) - 1
	value := (*values)[last]
	*values = (*values)[:last]
	return value
}

func retainSmallestPath(paths *maxStringHeap, retained map[string]struct{}, value string, maximum int) bool {
	if paths == nil || retained == nil || maximum < 1 {
		return false
	}
	if _, exists := retained[value]; exists {
		return true
	}
	if paths.Len() < maximum {
		heap.Push(paths, value)
		retained[value] = struct{}{}
		return true
	}
	if value >= (*paths)[0] {
		return true
	}
	removed := heap.Pop(paths).(string)
	delete(retained, removed)
	heap.Push(paths, value)
	retained[value] = struct{}{}
	return true
}

func decodeIndex(encoded []byte) ([]model.Record, map[string][]uint32, error) {
	records, postings, _, err := decodeIndexContextWithQueryObserved(context.Background(), encoded, nil)
	return records, postings, err
}

func decodeIndexContext(ctx context.Context, encoded []byte) ([]model.Record, map[string][]uint32, QueryIndex, error) {
	return decodeIndexContextWithQueryObserved(ctx, encoded, nil)
}

func decodeIndexContextWithQueryObserved(ctx context.Context, encoded []byte, observed func()) ([]model.Record, map[string][]uint32, QueryIndex, error) {
	invalid := func(err error) ([]model.Record, map[string][]uint32, QueryIndex, error) {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return nil, nil, QueryIndex{}, err
		}
		return nil, nil, QueryIndex{}, ErrInvalidIndex
	}
	plain, err := decompressIndexContext(ctx, encoded)
	if err != nil {
		return invalid(err)
	}
	budget := decodeMemoryBudget{used: int64(cap(encoded) + cap(plain) + conservativeZlibWorkspaceBytes)}
	if budget.used < 0 || budget.used > maximumIndexPeakBytes {
		return invalid(ErrInvalidIndex)
	}
	decoder := binaryDecoder{value: plain, budget: &budget}
	magic, err := decoder.readBytes(len(indexMagic))
	if err != nil || !bytes.Equal(magic, indexMagic) {
		return invalid(err)
	}
	version, err := decoder.readUint16()
	if err != nil || version != indexFormatVersion {
		return invalid(err)
	}
	recordCount, err := decoder.readCount(maximumIndexRecords)
	if err != nil || !decoder.canContain(recordCount, minimumEncodedRecordBytes()) || budget.reserve(int64(recordCount)*conservativeRecordMemoryBytes) != nil {
		return invalid(err)
	}
	records := make([]model.Record, 0, min(recordCount, 1024))
	totalOrdinals := 0
	for index := 0; index < recordCount; index++ {
		if index%contextCheckInterval == 0 {
			if observed != nil {
				observed()
			}
			if err := ctx.Err(); err != nil {
				return invalid(err)
			}
		}
		record, readErr := decoder.readRecord()
		if readErr != nil || (index > 0 && records[index-1].Identity >= record.Identity) {
			return invalid(readErr)
		}
		if totalOrdinals > maximumPostingOrdinals-len(record.SearchTerms) {
			return invalid(ErrInvalidIndex)
		}
		totalOrdinals += len(record.SearchTerms)
		records = append(records, record)
	}
	postingCount, err := decoder.readCount(maximumPostingTerms)
	if err != nil || !decoder.canContain(postingCount, 8) || budget.reserve(int64(postingCount)*64) != nil {
		return invalid(err)
	}
	postings := make(map[string][]uint32, postingCount)
	previousTerm, decodedOrdinals := "", 0
	for index := 0; index < postingCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return invalid(err)
			}
		}
		term, readErr := decoder.readString(128)
		if readErr != nil || !validTerm(term) || (index > 0 && previousTerm >= term) {
			return invalid(readErr)
		}
		ordinalCount, readErr := decoder.readCount(len(records))
		if readErr != nil || ordinalCount == 0 || !decoder.canContain(ordinalCount, 4) || decodedOrdinals > maximumPostingOrdinals-ordinalCount || budget.reserve(int64(ordinalCount)*4) != nil {
			return invalid(readErr)
		}
		decodedOrdinals += ordinalCount
		ordinals := make([]uint32, ordinalCount)
		for ordinalIndex := range ordinals {
			if ordinalIndex%contextCheckInterval == 0 {
				if err := ctx.Err(); err != nil {
					return invalid(err)
				}
			}
			ordinal, ordinalErr := decoder.readUint32()
			if ordinalErr != nil || uint64(ordinal) >= uint64(len(records)) || (ordinalIndex > 0 && ordinals[ordinalIndex-1] >= ordinal) {
				return invalid(ordinalErr)
			}
			if _, found := slices.BinarySearch(records[ordinal].SearchTerms, term); !found {
				return invalid(ErrInvalidIndex)
			}
			ordinals[ordinalIndex] = ordinal
		}
		postings[term] = ordinals
		previousTerm = term
	}
	if decodedOrdinals != totalOrdinals {
		return invalid(ErrInvalidIndex)
	}
	queryPostingCount, err := decoder.readCount(maximumQueryPostingTerms)
	if err != nil || !decoder.canContain(queryPostingCount, 8) || budget.reserve(int64(queryPostingCount)*conservativePostingGroupMemoryBytes) != nil {
		return invalid(err)
	}
	queryIndex, queryErr := buildQueryIndexContext(ctx, records, observed)
	if queryErr != nil {
		return invalid(queryErr)
	}
	if queryPostingCount != len(queryIndex.postings) {
		return invalid(ErrInvalidIndex)
	}
	expectedQueryOrdinals := 0
	for _, ordinals := range queryIndex.postings {
		if expectedQueryOrdinals > maximumQueryPostingOrdinals-len(ordinals) {
			return invalid(ErrInvalidIndex)
		}
		expectedQueryOrdinals += len(ordinals)
	}
	if budget.reserve(int64(expectedQueryOrdinals)*4) != nil {
		return invalid(ErrInvalidIndex)
	}
	previousQueryKey, decodedQueryOrdinals := "", 0
	for index := 0; index < queryPostingCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return invalid(err)
			}
		}
		key, readErr := decoder.readString(maximumQueryKeyBytes)
		if readErr != nil || !validQueryKey(key) || (index > 0 && previousQueryKey >= key) {
			return invalid(readErr)
		}
		expectedOrdinals, exists := queryIndex.postings[key]
		if !exists {
			return invalid(ErrInvalidIndex)
		}
		ordinalCount, readErr := decoder.readCount(len(records))
		if readErr != nil || ordinalCount == 0 || ordinalCount != len(expectedOrdinals) || decodedQueryOrdinals > maximumQueryPostingOrdinals-ordinalCount {
			return invalid(readErr)
		}
		decodedQueryOrdinals += ordinalCount
		rangeCount, readErr := decoder.readCount(ordinalCount)
		if readErr != nil || rangeCount == 0 || !decoder.canContain(rangeCount, 8) {
			return invalid(readErr)
		}
		ordinalIndex := 0
		var previousRangeEnd uint64
		for rangeIndex := 0; rangeIndex < rangeCount; rangeIndex++ {
			if rangeIndex%contextCheckInterval == 0 {
				if err := ctx.Err(); err != nil {
					return invalid(err)
				}
			}
			start, startErr := decoder.readUint32()
			length, lengthErr := decoder.readUint32()
			end := uint64(start) + uint64(length)
			if startErr != nil || lengthErr != nil || length == 0 || end > uint64(len(records)) || (rangeIndex > 0 && uint64(start) <= previousRangeEnd+1) || int(length) > ordinalCount-ordinalIndex {
				return invalid(ErrInvalidIndex)
			}
			for offset := uint32(0); offset < length; offset++ {
				if expectedOrdinals[ordinalIndex] != start+offset {
					return invalid(ErrInvalidIndex)
				}
				ordinalIndex++
			}
			previousRangeEnd = end - 1
		}
		if ordinalIndex != ordinalCount {
			return invalid(ErrInvalidIndex)
		}
		previousQueryKey = key
	}
	if decodedQueryOrdinals != expectedQueryOrdinals {
		return invalid(ErrInvalidIndex)
	}
	pathCount, err := decoder.readCount(len(records))
	if err != nil || pathCount != len(records) || !decoder.canContain(pathCount, 4) || budget.reserve(int64(pathCount)*5) != nil {
		return invalid(err)
	}
	seen := make([]byte, pathCount)
	if len(queryIndex.byPath) != pathCount {
		return invalid(ErrInvalidIndex)
	}
	for index := 0; index < pathCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return invalid(err)
			}
		}
		ordinal, readErr := decoder.readUint32()
		if readErr != nil || ordinal != queryIndex.byPath[index] || seen[ordinal] != 0 {
			return invalid(readErr)
		}
		seen[ordinal] = 1
	}
	expectedGroups, expectedPartial := queryIndex.mapGroups, queryIndex.mapPartial
	groupCount, err := decoder.readCount(policy.ProductionLimits().MaximumLexicalCandidates)
	if err != nil || groupCount != len(expectedGroups) || budget.reserve(int64(groupCount)*32) != nil {
		return invalid(err)
	}
	for index := 0; index < groupCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return invalid(err)
			}
		}
		path, readErr := decoder.readString(maximumIndexStringBytes)
		if readErr != nil || path != expectedGroups[index].Path {
			return invalid(readErr)
		}
		ordinalCount, readErr := decoder.readCount(len(records))
		if readErr != nil || ordinalCount != len(expectedGroups[index].Ordinals) || !decoder.canContain(ordinalCount, 4) || budget.reserve(int64(ordinalCount)*4) != nil {
			return invalid(readErr)
		}
		for ordinalIndex := 0; ordinalIndex < ordinalCount; ordinalIndex++ {
			ordinal, ordinalErr := decoder.readUint32()
			if ordinalErr != nil || ordinal != expectedGroups[index].Ordinals[ordinalIndex] {
				return invalid(ordinalErr)
			}
		}
	}
	partial, err := decoder.readUint32()
	if err != nil || partial > 1 || (partial == 1) != expectedPartial || decoder.remaining() != 0 {
		return invalid(err)
	}
	if err := ctx.Err(); err != nil {
		return invalid(err)
	}
	return records, postings, queryIndex, nil
}

func decompressIndexContext(ctx context.Context, encoded []byte) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(encoded) == 0 || len(encoded) > maximumEncodedIndexBytes {
		return nil, ErrInvalidIndex
	}
	// Small compressed inputs can still expand to the full plain-text ceiling.
	// Measure those streams first so hostile high-ratio payloads need one exact
	// retained allocation instead of the cumulative bytes.Buffer growth series.
	if len(encoded) <= exactDecompressionSizingThreshold {
		plainSize, err := measureDecompressedIndexContext(ctx, encoded)
		if err != nil {
			return nil, err
		}
		return decompressExactIndexContext(ctx, encoded, plainSize)
	}
	compressed := bytes.NewReader(encoded)
	reader, err := zlib.NewReader(compressed)
	if err != nil {
		return nil, ErrInvalidIndex
	}
	var plain bytes.Buffer
	buffer := make([]byte, 32<<10)
	for {
		if err := ctx.Err(); err != nil {
			_ = reader.Close()
			return nil, err
		}
		count, readErr := reader.Read(buffer)
		if count > 0 {
			if plain.Len() > maximumDecompressedIndexBytes-count {
				_ = reader.Close()
				return nil, ErrInvalidIndex
			}
			_, _ = plain.Write(buffer[:count])
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = reader.Close()
			return nil, ErrInvalidIndex
		}
	}
	if err := reader.Close(); err != nil || compressed.Len() != 0 {
		return nil, ErrInvalidIndex
	}
	return plain.Bytes(), nil
}

func measureDecompressedIndexContext(ctx context.Context, encoded []byte) (int, error) {
	compressed := bytes.NewReader(encoded)
	reader, err := zlib.NewReader(compressed)
	if err != nil {
		return 0, ErrInvalidIndex
	}
	buffer := make([]byte, 32<<10)
	total := 0
	for {
		if err := ctx.Err(); err != nil {
			_ = reader.Close()
			return 0, err
		}
		count, readErr := reader.Read(buffer)
		if count > 0 {
			if total > maximumDecompressedIndexBytes-count {
				_ = reader.Close()
				return 0, ErrInvalidIndex
			}
			total += count
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = reader.Close()
			return 0, ErrInvalidIndex
		}
	}
	if err := reader.Close(); err != nil || compressed.Len() != 0 {
		return 0, ErrInvalidIndex
	}
	return total, nil
}

func decompressExactIndexContext(ctx context.Context, encoded []byte, plainSize int) ([]byte, error) {
	compressed := bytes.NewReader(encoded)
	reader, err := zlib.NewReader(compressed)
	if err != nil {
		return nil, ErrInvalidIndex
	}
	plain := make([]byte, plainSize)
	written := 0
	var overflow [1]byte
	for {
		if err := ctx.Err(); err != nil {
			_ = reader.Close()
			return nil, err
		}
		end := min(len(plain), written+(32<<10))
		var destination []byte
		if written < len(plain) {
			destination = plain[written:end]
		} else {
			destination = overflow[:]
		}
		count, readErr := reader.Read(destination)
		written += count
		if written > len(plain) {
			_ = reader.Close()
			return nil, ErrInvalidIndex
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = reader.Close()
			return nil, ErrInvalidIndex
		}
	}
	if err := reader.Close(); err != nil || compressed.Len() != 0 || written != len(plain) {
		return nil, ErrInvalidIndex
	}
	return plain, nil
}

type rawRecordTerms struct {
	offset uint32
	count  uint8
}

type rawField struct {
	offset uint32
	length uint32
}

type rawRecordMetadata struct {
	identity, path, language, kind, source, qualified, evidence rawField
	terms                                                       rawRecordTerms
	start                                                       uint32
}

func (field rawField) bytes(plain []byte) []byte {
	end := uint64(field.offset) + uint64(field.length)
	if uint64(field.offset) > uint64(len(plain)) || end > uint64(len(plain)) {
		return nil
	}
	return plain[field.offset:uint32(end)]
}

func rawFieldAt(decoder *rawBinaryDecoder, value []byte) rawField {
	return rawField{offset: uint32(decoder.offset - len(value)), length: uint32(len(value))}
}

// validateIndex verifies the complete canonical index without materializing
// records, copied strings, or postings. The only representation beyond the
// bounded plain bytes is one compact term-section locator per record.
func validateIndex(encoded []byte) (int, int, error) {
	return validateIndexContext(context.Background(), encoded)
}

func validateIndexContext(ctx context.Context, encoded []byte) (int, int, error) {
	plain, err := decompressIndexContext(ctx, encoded)
	if err != nil {
		return 0, 0, err
	}
	decoder := rawBinaryDecoder{value: plain}
	magic, err := decoder.readBytes(len(indexMagic))
	if err != nil || !bytes.Equal(magic, indexMagic) {
		return 0, 0, ErrInvalidIndex
	}
	version, err := decoder.readUint16()
	if err != nil || version != indexFormatVersion {
		return 0, 0, ErrInvalidIndex
	}
	recordCount, err := decoder.readCount(maximumIndexRecords)
	if err != nil || !decoder.canContain(recordCount, minimumEncodedRecordBytes()) {
		return 0, 0, ErrInvalidIndex
	}
	peak := int64(cap(encoded) + cap(plain) + conservativeZlibWorkspaceBytes)
	if locations := int64(recordCount) * 72; peak < 0 || locations < 0 || peak > maximumIndexPeakBytes-locations {
		return 0, 0, ErrInvalidIndex
	}
	records := make([]rawRecordMetadata, recordCount)
	var previousIdentity []byte
	totalOrdinals, expectedQueryOrdinals := 0, 0
	for recordIndex := 0; recordIndex < recordCount; recordIndex++ {
		if recordIndex%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return 0, 0, err
			}
		}
		identity, err := decoder.readString(71)
		if err != nil || !validSHA256IdentityBytes(identity) || (recordIndex > 0 && bytes.Compare(previousIdentity, identity) >= 0) {
			return 0, 0, ErrInvalidIndex
		}
		previousIdentity = identity
		metadata := rawRecordMetadata{identity: rawFieldAt(&decoder, identity)}
		pathValue, err := decoder.readString(maximumIndexStringBytes)
		if err != nil || !validRelativePathBytes(pathValue) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.path = rawFieldAt(&decoder, pathValue)
		start, startErr := decoder.readUint32()
		end, endErr := decoder.readUint32()
		if startErr != nil || endErr != nil || start < 1 || end < start || uint64(end) > uint64(math.MaxInt) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.start = start
		language, err := decoder.readString(128)
		if err != nil || !validTextBytes(language, 128, false) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.language = rawFieldAt(&decoder, language)
		kind, err := decoder.readString(32)
		if err != nil || !validRecordKindBytes(kind) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.kind = rawFieldAt(&decoder, kind)
		sourceType, err := decoder.readString(32)
		if err != nil || !validSourceTypeBytes(sourceType) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.source = rawFieldAt(&decoder, sourceType)
		qualified, err := decoder.readString(512)
		if err != nil || !validTextBytes(qualified, 512, false) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.qualified = rawFieldAt(&decoder, qualified)
		extraction, err := decoder.readString(512)
		if err != nil || !validTextBytes(extraction, 512, false) {
			return 0, 0, ErrInvalidIndex
		}
		evidence, err := decoder.readString(16)
		if err != nil || !validEvidenceClassBytes(evidence) {
			return 0, 0, ErrInvalidIndex
		}
		metadata.evidence = rawFieldAt(&decoder, evidence)
		termCount, err := decoder.readCount(maximumTermsPerRecord)
		if err != nil || !decoder.canContain(termCount, 4) || totalOrdinals > maximumPostingOrdinals-termCount {
			return 0, 0, ErrInvalidIndex
		}
		totalOrdinals += termCount
		metadata.terms = rawRecordTerms{offset: uint32(decoder.offset), count: uint8(termCount)}
		var previousTerm []byte
		for termIndex := 0; termIndex < termCount; termIndex++ {
			term, err := decoder.readString(128)
			if err != nil || !validTermBytes(term) || (termIndex > 0 && bytes.Compare(previousTerm, term) >= 0) {
				return 0, 0, ErrInvalidIndex
			}
			previousTerm = term
		}
		digest, err := decoder.readString(71)
		if err != nil || !validSHA256IdentityBytes(digest) {
			return 0, 0, ErrInvalidIndex
		}
		preview, err := decoder.readString(maximumIndexStringBytes)
		if err != nil || !validTextBytes(preview, maximumIndexStringBytes, true) {
			return 0, 0, ErrInvalidIndex
		}
		count, err := rawQueryKeyCount(plain, metadata)
		if err != nil || expectedQueryOrdinals > maximumQueryPostingOrdinals-count {
			return 0, 0, ErrInvalidIndex
		}
		expectedQueryOrdinals += count
		records[recordIndex] = metadata
	}
	postingCount, err := decoder.readCount(maximumPostingTerms)
	if err != nil || !decoder.canContain(postingCount, 8) {
		return 0, 0, ErrInvalidIndex
	}
	decodedOrdinals := 0
	var previousPostingTerm []byte
	for postingIndex := 0; postingIndex < postingCount; postingIndex++ {
		if postingIndex%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return 0, 0, err
			}
		}
		term, err := decoder.readString(128)
		if err != nil || !validTermBytes(term) || (postingIndex > 0 && bytes.Compare(previousPostingTerm, term) >= 0) {
			return 0, 0, ErrInvalidIndex
		}
		previousPostingTerm = term
		ordinalCount, err := decoder.readCount(recordCount)
		if err != nil || ordinalCount == 0 || !decoder.canContain(ordinalCount, 4) || decodedOrdinals > maximumPostingOrdinals-ordinalCount {
			return 0, 0, ErrInvalidIndex
		}
		decodedOrdinals += ordinalCount
		var previousOrdinal uint32
		for ordinalIndex := 0; ordinalIndex < ordinalCount; ordinalIndex++ {
			if ordinalIndex%contextCheckInterval == 0 {
				if err := ctx.Err(); err != nil {
					return 0, 0, err
				}
			}
			ordinal, err := decoder.readUint32()
			if err != nil || uint64(ordinal) >= uint64(recordCount) || (ordinalIndex > 0 && previousOrdinal >= ordinal) ||
				!rawRecordContainsTerm(plain, records[ordinal].terms, term) {
				return 0, 0, ErrInvalidIndex
			}
			previousOrdinal = ordinal
		}
	}
	if decodedOrdinals != totalOrdinals {
		return 0, 0, ErrInvalidIndex
	}
	queryPostingCount, err := decoder.readCount(maximumQueryPostingTerms)
	if err != nil || !decoder.canContain(queryPostingCount, 8) {
		return 0, 0, ErrInvalidIndex
	}
	decodedQueryOrdinals := 0
	var previousQueryKey []byte
	for postingIndex := 0; postingIndex < queryPostingCount; postingIndex++ {
		if postingIndex%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return 0, 0, err
			}
		}
		key, err := decoder.readString(maximumQueryKeyBytes)
		if err != nil || !validQueryKey(string(key)) || (postingIndex > 0 && bytes.Compare(previousQueryKey, key) >= 0) {
			return 0, 0, ErrInvalidIndex
		}
		previousQueryKey = key
		ordinalCount, err := decoder.readCount(recordCount)
		if err != nil || ordinalCount == 0 || decodedQueryOrdinals > maximumQueryPostingOrdinals-ordinalCount {
			return 0, 0, ErrInvalidIndex
		}
		decodedQueryOrdinals += ordinalCount
		rangeCount, err := decoder.readCount(ordinalCount)
		if err != nil || rangeCount == 0 || !decoder.canContain(rangeCount, 8) {
			return 0, 0, ErrInvalidIndex
		}
		visited := 0
		var previousRangeEnd uint64
		for rangeIndex := 0; rangeIndex < rangeCount; rangeIndex++ {
			if rangeIndex%contextCheckInterval == 0 {
				if err := ctx.Err(); err != nil {
					return 0, 0, err
				}
			}
			start, startErr := decoder.readUint32()
			length, lengthErr := decoder.readUint32()
			end := uint64(start) + uint64(length)
			if startErr != nil || lengthErr != nil || length == 0 || end > uint64(recordCount) || (rangeIndex > 0 && uint64(start) <= previousRangeEnd+1) || int(length) > ordinalCount-visited {
				return 0, 0, ErrInvalidIndex
			}
			for offset := uint32(0); offset < length; offset++ {
				if offset%contextCheckInterval == 0 {
					if err := ctx.Err(); err != nil {
						return 0, 0, err
					}
				}
				ordinal := start + offset
				if !rawRecordHasQueryKey(plain, records[ordinal], key) {
					return 0, 0, ErrInvalidIndex
				}
				visited++
			}
			previousRangeEnd = end - 1
		}
		if visited != ordinalCount {
			return 0, 0, ErrInvalidIndex
		}
	}
	if decodedQueryOrdinals != expectedQueryOrdinals {
		return 0, 0, ErrInvalidIndex
	}
	pathCount, err := decoder.readCount(recordCount)
	if err != nil || pathCount != recordCount || !decoder.canContain(pathCount, 4) || peak > maximumIndexPeakBytes-int64(pathCount)*5 {
		return 0, 0, ErrInvalidIndex
	}
	seen := make([]byte, pathCount)
	pathOrdinals := make([]uint32, 0, pathCount)
	var previousPathOrdinal uint32
	for index := 0; index < pathCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return 0, 0, err
			}
		}
		ordinal, err := decoder.readUint32()
		if err != nil || uint64(ordinal) >= uint64(recordCount) || seen[ordinal] != 0 || (index > 0 && rawComparePathOrdinal(plain, records, previousPathOrdinal, ordinal) >= 0) {
			return 0, 0, ErrInvalidIndex
		}
		seen[ordinal] = 1
		previousPathOrdinal = ordinal
		pathOrdinals = append(pathOrdinals, ordinal)
	}
	expectedGroups, expectedPartial, err := rawMapGroupsContext(ctx, plain, records, pathOrdinals, pathCount, policy.ProductionLimits().MaximumLexicalCandidates)
	if err != nil {
		return 0, 0, err
	}
	groupCount, err := decoder.readCount(policy.ProductionLimits().MaximumLexicalCandidates)
	if err != nil || groupCount != len(expectedGroups) {
		return 0, 0, ErrInvalidIndex
	}
	for index := 0; index < groupCount; index++ {
		if index%contextCheckInterval == 0 {
			if err := ctx.Err(); err != nil {
				return 0, 0, err
			}
		}
		path, err := decoder.readString(maximumIndexStringBytes)
		if err != nil || !bytes.Equal(path, expectedGroups[index].path) {
			return 0, 0, ErrInvalidIndex
		}
		ordinalCount, err := decoder.readCount(recordCount)
		if err != nil || ordinalCount != 1 || !decoder.canContain(ordinalCount, 4) {
			return 0, 0, ErrInvalidIndex
		}
		ordinal, err := decoder.readUint32()
		if err != nil || ordinal != expectedGroups[index].ordinal {
			return 0, 0, ErrInvalidIndex
		}
	}
	partial, err := decoder.readUint32()
	if err != nil || partial > 1 || (partial == 1) != expectedPartial || decoder.remaining() != 0 {
		return 0, 0, ErrInvalidIndex
	}
	if err := ctx.Err(); err != nil {
		return 0, 0, err
	}
	return recordCount, postingCount, nil
}

type rawBinaryDecoder struct {
	value  []byte
	offset int
}

func (decoder *rawBinaryDecoder) remaining() int { return len(decoder.value) - decoder.offset }

func (decoder *rawBinaryDecoder) canContain(count, minimum int) bool {
	return count >= 0 && minimum >= 0 && (count == 0 || count <= decoder.remaining()/minimum)
}

func (decoder *rawBinaryDecoder) readBytes(length int) ([]byte, error) {
	if length < 0 || length > decoder.remaining() {
		return nil, ErrInvalidIndex
	}
	result := decoder.value[decoder.offset : decoder.offset+length]
	decoder.offset += length
	return result, nil
}

func (decoder *rawBinaryDecoder) readUint16() (uint16, error) {
	raw, err := decoder.readBytes(2)
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint16(raw), nil
}

func (decoder *rawBinaryDecoder) readUint32() (uint32, error) {
	raw, err := decoder.readBytes(4)
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint32(raw), nil
}

func (decoder *rawBinaryDecoder) readCount(maximum int) (int, error) {
	value, err := decoder.readUint32()
	if err != nil || uint64(value) > uint64(maximum) || uint64(value) > uint64(math.MaxInt) {
		return 0, ErrInvalidIndex
	}
	return int(value), nil
}

func (decoder *rawBinaryDecoder) readString(maximum int) ([]byte, error) {
	length, err := decoder.readCount(maximum)
	if err != nil || length > decoder.remaining() {
		return nil, ErrInvalidIndex
	}
	value, err := decoder.readBytes(length)
	if err != nil || !utf8.Valid(value) {
		return nil, ErrInvalidIndex
	}
	return value, nil
}

func rawRecordContainsTerm(plain []byte, section rawRecordTerms, term []byte) bool {
	decoder := rawBinaryDecoder{value: plain, offset: int(section.offset)}
	for index := 0; index < int(section.count); index++ {
		candidate, err := decoder.readString(128)
		if err != nil {
			return false
		}
		comparison := bytes.Compare(candidate, term)
		if comparison == 0 {
			return true
		}
		if comparison > 0 {
			return false
		}
	}
	return false
}

func rawModelRecord(plain []byte, metadata rawRecordMetadata) (model.Record, error) {
	record := model.Record{
		Identity: string(metadata.identity.bytes(plain)), Path: string(metadata.path.bytes(plain)), StartLine: int(metadata.start),
		Language: string(metadata.language.bytes(plain)), RecordKind: model.RecordKind(metadata.kind.bytes(plain)),
		SourceType: string(metadata.source.bytes(plain)), QualifiedName: string(metadata.qualified.bytes(plain)),
		EvidenceClass: model.EvidenceClass(metadata.evidence.bytes(plain)),
	}
	decoder := rawBinaryDecoder{value: plain, offset: int(metadata.terms.offset)}
	record.SearchTerms = make([]string, int(metadata.terms.count))
	for index := range record.SearchTerms {
		term, err := decoder.readString(128)
		if err != nil {
			return model.Record{}, ErrInvalidIndex
		}
		record.SearchTerms[index] = string(term)
	}
	return record, nil
}

func rawQueryKeyCount(plain []byte, metadata rawRecordMetadata) (int, error) {
	record, err := rawModelRecord(plain, metadata)
	if err != nil {
		return 0, err
	}
	return len(canonicalQueryKeys(record)), nil
}

func rawRecordHasQueryKey(plain []byte, metadata rawRecordMetadata, key []byte) bool {
	record, err := rawModelRecord(plain, metadata)
	return err == nil && recordHasQueryKey(record, string(key))
}

func rawComparePathOrdinal(plain []byte, records []rawRecordMetadata, left, right uint32) int {
	l, r := records[left], records[right]
	for _, comparison := range []int{
		cmp.Compare(NormalizeQueryText(string(l.path.bytes(plain))), NormalizeQueryText(string(r.path.bytes(plain)))),
		bytes.Compare(l.path.bytes(plain), r.path.bytes(plain)),
		cmp.Compare(l.start, r.start),
		bytes.Compare(l.kind.bytes(plain), r.kind.bytes(plain)),
		cmp.Compare(NormalizeQueryText(string(l.qualified.bytes(plain))), NormalizeQueryText(string(r.qualified.bytes(plain)))),
		bytes.Compare(l.identity.bytes(plain), r.identity.bytes(plain)),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

type rawMapGroup struct {
	path    []byte
	ordinal uint32
}

func rawMapGroupsContext(ctx context.Context, plain []byte, records []rawRecordMetadata, pathOrdinals []uint32, pathCount, maximum int) ([]rawMapGroup, bool, error) {
	groups := make([]rawMapGroup, 0, min(pathCount, maximum))
	partial := false
	visited := 0
	for start := 0; start < len(pathOrdinals); {
		end := start + 1
		path := records[pathOrdinals[start]].path.bytes(plain)
		best := pathOrdinals[start]
		for end < len(pathOrdinals) && bytes.Equal(records[pathOrdinals[end]].path.bytes(plain), path) {
			visited++
			if visited%contextCheckInterval == 0 {
				if err := ctx.Err(); err != nil {
					return nil, false, err
				}
			}
			if rawCompareMapRepresentative(plain, records[pathOrdinals[end]], records[best]) < 0 {
				best = pathOrdinals[end]
			}
			end++
		}
		if len(groups) == maximum {
			partial = true
			break
		}
		groups = append(groups, rawMapGroup{path: path, ordinal: best})
		start = end
	}
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	return groups, partial, nil
}

func rawCompareMapRepresentative(plain []byte, left, right rawRecordMetadata) int {
	for _, comparison := range []int{
		cmp.Compare(rawEvidenceTier(left.evidence.bytes(plain)), rawEvidenceTier(right.evidence.bytes(plain))),
		cmp.Compare(rawSourceTier(left.source.bytes(plain)), rawSourceTier(right.source.bytes(plain))),
		cmp.Compare(rawKindTier(left.kind.bytes(plain)), rawKindTier(right.kind.bytes(plain))),
		cmp.Compare(left.start, right.start),
		cmp.Compare(NormalizeQueryText(string(left.qualified.bytes(plain))), NormalizeQueryText(string(right.qualified.bytes(plain)))),
		bytes.Compare(left.identity.bytes(plain), right.identity.bytes(plain)),
	} {
		if comparison != 0 {
			return comparison
		}
	}
	return 0
}

func rawEvidenceTier(value []byte) int {
	if bytes.Equal(value, []byte(model.Verified)) {
		return 0
	}
	if bytes.Equal(value, []byte(model.Inferred)) {
		return 1
	}
	return 2
}

func rawSourceTier(value []byte) int {
	if bytes.Equal(value, []byte("source")) {
		return 0
	}
	if bytes.Equal(value, []byte("document")) {
		return 1
	}
	return 2
}

func rawKindTier(value []byte) int {
	if bytes.Equal(value, []byte(model.Module)) {
		return 0
	}
	if bytes.Equal(value, []byte(model.Heading)) {
		return 1
	}
	if bytes.Equal(value, []byte(model.DocumentChunk)) {
		return 2
	}
	return 3
}

func skipRawRecord(decoder *rawBinaryDecoder) error {
	if decoder == nil {
		return ErrInvalidIndex
	}
	for _, maximum := range []int{71, maximumIndexStringBytes} {
		if _, err := decoder.readString(maximum); err != nil {
			return err
		}
	}
	if _, err := decoder.readUint32(); err != nil {
		return err
	}
	if _, err := decoder.readUint32(); err != nil {
		return err
	}
	for _, maximum := range []int{128, 32, 32, 512, 512, 16} {
		if _, err := decoder.readString(maximum); err != nil {
			return err
		}
	}
	termCount, err := decoder.readCount(maximumTermsPerRecord)
	if err != nil {
		return err
	}
	for range termCount {
		if _, err := decoder.readString(128); err != nil {
			return err
		}
	}
	for _, maximum := range []int{71, maximumIndexStringBytes} {
		if _, err := decoder.readString(maximum); err != nil {
			return err
		}
	}
	return nil
}

func validSHA256IdentityBytes(value []byte) bool {
	if len(value) != 71 || !bytes.Equal(value[:7], []byte("sha256:")) {
		return false
	}
	for _, character := range value[7:] {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func validRecordKindBytes(value []byte) bool {
	return bytes.Equal(value, []byte(model.Module)) || bytes.Equal(value, []byte(model.Definition)) ||
		bytes.Equal(value, []byte(model.Import)) || bytes.Equal(value, []byte(model.EntryPoint)) ||
		bytes.Equal(value, []byte(model.Configuration)) || bytes.Equal(value, []byte(model.Heading)) ||
		bytes.Equal(value, []byte(model.DocumentChunk))
}

func validEvidenceClassBytes(value []byte) bool {
	return bytes.Equal(value, []byte(model.Verified)) || bytes.Equal(value, []byte(model.Inferred)) || bytes.Equal(value, []byte(model.Uncertain))
}

func validSourceTypeBytes(value []byte) bool {
	return bytes.Equal(value, []byte("source")) || bytes.Equal(value, []byte("document")) || bytes.Equal(value, []byte("configuration"))
}

func validTermBytes(value []byte) bool {
	if !validTextBytes(value, 128, false) || !bytes.Equal(bytes.TrimSpace(value), value) || bytes.HasPrefix(value, []byte(queryNamespace)) {
		return false
	}
	for remaining := value; len(remaining) != 0; {
		runeValue, size := utf8.DecodeRune(remaining)
		if unicode.ToLower(runeValue) != runeValue {
			return false
		}
		remaining = remaining[size:]
	}
	return true
}

func validRelativePathBytes(value []byte) bool {
	if !validTextBytes(value, maximumIndexStringBytes, false) || bytes.ContainsRune(value, '\\') || bytes.ContainsRune(value, ':') || value[0] == '/' {
		return false
	}
	for start := 0; start <= len(value); {
		end := start
		for end < len(value) && value[end] != '/' {
			end++
		}
		if end == start || (end-start == 1 && value[start] == '.') || (end-start == 2 && value[start] == '.' && value[start+1] == '.') {
			return false
		}
		if end == len(value) {
			break
		}
		start = end + 1
	}
	return true
}

func validTextBytes(value []byte, maximum int, empty bool) bool {
	if !utf8.Valid(value) || len(value) > maximum || (!empty && len(value) == 0) {
		return false
	}
	for len(value) != 0 {
		runeValue, size := utf8.DecodeRune(value)
		if unicode.IsControl(runeValue) {
			return false
		}
		value = value[size:]
	}
	return true
}

func validRecord(record model.Record) bool {
	valid, _ := validRecordContext(context.Background(), record, nil)
	return valid
}

func validRecordContext(ctx context.Context, record model.Record, observed func(buildPhase)) (bool, error) {
	if !validSHA256IdentityString(record.Identity) || record.StartLine < 1 || record.EndLine < record.StartLine || uint64(record.EndLine) > math.MaxUint32 ||
		!validRecordKind(record.RecordKind) || !validSourceType(record.SourceType) || !validEvidenceClass(record.EvidenceClass) || !validSHA256IdentityString(record.SourceDigest) {
		return false, ctx.Err()
	}
	valid, err := validRelativePathContext(ctx, record.Path, observed)
	if err != nil || !valid {
		return valid, err
	}
	for _, field := range []struct {
		value   string
		maximum int
		empty   bool
	}{
		{value: record.Language, maximum: 128},
		{value: record.QualifiedName, maximum: 512},
		{value: record.ExtractionMethod, maximum: 512},
		{value: record.Preview, maximum: maximumIndexStringBytes, empty: true},
	} {
		valid, err = validTextContext(ctx, field.value, field.maximum, field.empty, observed)
		if err != nil || !valid {
			return valid, err
		}
	}
	return true, ctx.Err()
}

func validSHA256IdentityString(value string) bool {
	if len(value) != 71 || value[:7] != "sha256:" {
		return false
	}
	for index := 7; index < len(value); index++ {
		character := value[index]
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func validRecordKind(kind model.RecordKind) bool {
	switch kind {
	case model.Module, model.Definition, model.Import, model.EntryPoint, model.Configuration, model.Heading, model.DocumentChunk:
		return true
	default:
		return false
	}
}

func validEvidenceClass(evidence model.EvidenceClass) bool {
	return evidence == model.Verified || evidence == model.Inferred || evidence == model.Uncertain
}

func validSourceType(value string) bool {
	return value == "source" || value == "document" || value == "configuration"
}

func validTerm(value string) bool {
	valid, _ := validTermContext(context.Background(), value, nil)
	return valid
}

func validTermContext(ctx context.Context, value string, observed func(buildPhase)) (bool, error) {
	valid, err := validTextContext(ctx, value, 128, false, observed)
	if err != nil || !valid {
		return valid, err
	}
	return value == strings.TrimSpace(value) && value == strings.ToLower(value) && !strings.HasPrefix(value, queryNamespace), ctx.Err()
}

func validRelativePath(value string) bool {
	valid, _ := validRelativePathContext(context.Background(), value, nil)
	return valid
}

func validRelativePathContext(ctx context.Context, value string, observed func(buildPhase)) (bool, error) {
	valid, err := validTextContext(ctx, value, maximumIndexStringBytes, false, observed)
	if err != nil || !valid || strings.HasPrefix(value, "/") {
		return false, err
	}
	nextCheck := validationByteCheckInterval
	for start := 0; start <= len(value); {
		if start >= nextCheck {
			if err := observeBuildContext(ctx, observed, buildPhaseValidation); err != nil {
				return false, err
			}
			nextCheck = start + validationByteCheckInterval
		}
		end := start
		for end < len(value) && value[end] != '/' {
			if end >= nextCheck {
				if err := observeBuildContext(ctx, observed, buildPhaseValidation); err != nil {
					return false, err
				}
				nextCheck = end + validationByteCheckInterval
			}
			if value[end] == '\\' || value[end] == ':' {
				return false, ctx.Err()
			}
			end++
		}
		if end == start || (end-start == 1 && value[start] == '.') || (end-start == 2 && value[start] == '.' && value[start+1] == '.') {
			return false, ctx.Err()
		}
		if end == len(value) {
			break
		}
		start = end + 1
	}
	return true, ctx.Err()
}

func validText(value string, maximum int, empty bool) bool {
	valid, _ := validTextContext(context.Background(), value, maximum, empty, nil)
	return valid
}

func validTextContext(ctx context.Context, value string, maximum int, empty bool, observed func(buildPhase)) (bool, error) {
	if len(value) > maximum || (!empty && len(value) == 0) {
		return false, ctx.Err()
	}
	nextCheck := validationByteCheckInterval
	for offset := 0; offset < len(value); {
		if offset >= nextCheck {
			if err := observeBuildContext(ctx, observed, buildPhaseValidation); err != nil {
				return false, err
			}
			nextCheck += validationByteCheckInterval
		}
		runeValue, size := utf8.DecodeRuneInString(value[offset:])
		if (runeValue == utf8.RuneError && size == 1) || unicode.IsControl(runeValue) {
			return false, ctx.Err()
		}
		offset += size
	}
	return true, ctx.Err()
}

func minimumEncodedRecordBytes() int { return 10*4 + 3*4 }

func writeRecord(writer *bytes.Buffer, record model.Record) {
	writeString(writer, record.Identity)
	writeString(writer, record.Path)
	writeUint32(writer, uint32(record.StartLine))
	writeUint32(writer, uint32(record.EndLine))
	writeString(writer, record.Language)
	writeString(writer, string(record.RecordKind))
	writeString(writer, record.SourceType)
	writeString(writer, record.QualifiedName)
	writeString(writer, record.ExtractionMethod)
	writeString(writer, string(record.EvidenceClass))
	writeUint32(writer, uint32(len(record.SearchTerms)))
	for _, term := range record.SearchTerms {
		writeString(writer, term)
	}
	writeString(writer, record.SourceDigest)
	writeString(writer, record.Preview)
}

func writeCanonicalRecord(writer *bytes.Buffer, record model.Record) {
	writeString(writer, record.Identity)
	writeString(writer, record.Path)
	writeUint32(writer, uint32(record.StartLine))
	writeUint32(writer, uint32(record.EndLine))
	writeString(writer, record.Language)
	writeString(writer, string(record.RecordKind))
	writeString(writer, record.SourceType)
	writeString(writer, record.QualifiedName)
	writeString(writer, record.ExtractionMethod)
	writeString(writer, string(record.EvidenceClass))
	writeUint32(writer, uint32(len(record.SearchTerms)))
	order := canonicalTermOrder(record.SearchTerms)
	for _, termIndex := range order[:len(record.SearchTerms)] {
		writeString(writer, record.SearchTerms[termIndex])
	}
	writeString(writer, record.SourceDigest)
	writeString(writer, record.Preview)
}

func canonicalTermOrder(terms []string) [maximumTermsPerRecord]uint8 {
	var order [maximumTermsPerRecord]uint8
	for index := range terms {
		position := index
		for position > 0 && terms[order[position-1]] > terms[index] {
			order[position] = order[position-1]
			position--
		}
		order[position] = uint8(index)
	}
	return order
}

func writeUint16(writer *bytes.Buffer, value uint16) {
	var raw [2]byte
	binary.BigEndian.PutUint16(raw[:], value)
	writer.Write(raw[:])
}

func writeUint32(writer *bytes.Buffer, value uint32) {
	var raw [4]byte
	binary.BigEndian.PutUint32(raw[:], value)
	writer.Write(raw[:])
}

func writeString(writer *bytes.Buffer, value string) {
	writeUint32(writer, uint32(len(value)))
	writer.WriteString(value)
}

type binaryDecoder struct {
	value  []byte
	offset int
	budget *decodeMemoryBudget
}

type decodeMemoryBudget struct{ used int64 }

func (budget *decodeMemoryBudget) reserve(amount int64) error {
	if budget == nil || amount < 0 || budget.used < 0 || amount > maximumIndexPeakBytes-budget.used {
		return ErrInvalidIndex
	}
	budget.used += amount
	return nil
}

func (decoder *binaryDecoder) remaining() int { return len(decoder.value) - decoder.offset }

func (decoder *binaryDecoder) canContain(count, minimum int) bool {
	return count >= 0 && minimum >= 0 && (count == 0 || count <= decoder.remaining()/minimum)
}

func (decoder *binaryDecoder) readBytes(length int) ([]byte, error) {
	if length < 0 || length > decoder.remaining() {
		return nil, ErrInvalidIndex
	}
	result := decoder.value[decoder.offset : decoder.offset+length]
	decoder.offset += length
	return result, nil
}

func (decoder *binaryDecoder) readUint16() (uint16, error) {
	raw, err := decoder.readBytes(2)
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint16(raw), nil
}

func (decoder *binaryDecoder) readUint32() (uint32, error) {
	raw, err := decoder.readBytes(4)
	if err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint32(raw), nil
}

func (decoder *binaryDecoder) readCount(maximum int) (int, error) {
	value, err := decoder.readUint32()
	if err != nil || uint64(value) > uint64(maximum) || uint64(value) > uint64(math.MaxInt) {
		return 0, ErrInvalidIndex
	}
	return int(value), nil
}

func (decoder *binaryDecoder) readString(maximum int) (string, error) {
	length, err := decoder.readCount(maximum)
	if err != nil || length > decoder.remaining() {
		return "", ErrInvalidIndex
	}
	raw, err := decoder.readBytes(length)
	if err != nil || !utf8.Valid(raw) || decoder.budget.reserve(int64(length)+conservativeStringAllocationOverhead) != nil {
		return "", ErrInvalidIndex
	}
	return string(raw), nil
}

func (decoder *binaryDecoder) readRecord() (model.Record, error) {
	var record model.Record
	var err error
	if record.Identity, err = decoder.readString(71); err != nil {
		return model.Record{}, err
	}
	if record.Path, err = decoder.readString(maximumIndexStringBytes); err != nil {
		return model.Record{}, err
	}
	start, err := decoder.readUint32()
	if err != nil || uint64(start) > uint64(math.MaxInt) {
		return model.Record{}, ErrInvalidIndex
	}
	end, err := decoder.readUint32()
	if err != nil || uint64(end) > uint64(math.MaxInt) {
		return model.Record{}, ErrInvalidIndex
	}
	record.StartLine, record.EndLine = int(start), int(end)
	if record.Language, err = decoder.readString(128); err != nil {
		return model.Record{}, err
	}
	kind, err := decoder.readString(32)
	if err != nil {
		return model.Record{}, err
	}
	record.RecordKind = model.RecordKind(kind)
	if record.SourceType, err = decoder.readString(32); err != nil {
		return model.Record{}, err
	}
	if record.QualifiedName, err = decoder.readString(512); err != nil {
		return model.Record{}, err
	}
	if record.ExtractionMethod, err = decoder.readString(512); err != nil {
		return model.Record{}, err
	}
	evidence, err := decoder.readString(16)
	if err != nil {
		return model.Record{}, err
	}
	record.EvidenceClass = model.EvidenceClass(evidence)
	termCount, err := decoder.readCount(maximumTermsPerRecord)
	if err != nil || !decoder.canContain(termCount, 4) {
		return model.Record{}, ErrInvalidIndex
	}
	if decoder.budget.reserve(int64(termCount)*16) != nil {
		return model.Record{}, ErrInvalidIndex
	}
	record.SearchTerms = make([]string, termCount)
	for index := range record.SearchTerms {
		term, err := decoder.readString(128)
		if err != nil || !validTerm(term) || (index > 0 && record.SearchTerms[index-1] >= term) {
			return model.Record{}, ErrInvalidIndex
		}
		record.SearchTerms[index] = term
	}
	if record.SourceDigest, err = decoder.readString(71); err != nil {
		return model.Record{}, err
	}
	if record.Preview, err = decoder.readString(maximumIndexStringBytes); err != nil {
		return model.Record{}, err
	}
	if !validRecord(record) {
		return model.Record{}, ErrInvalidIndex
	}
	return record, nil
}

type manifestJSON struct {
	FormatVersion           string            `json:"format_version"`
	EngineVersion           string            `json:"engine_version"`
	Binding                 bindingJSON       `json:"binding"`
	InclusionPolicyIdentity string            `json:"inclusion_policy_identity"`
	ExclusionPolicyIdentity string            `json:"exclusion_policy_identity"`
	ParserIdentities        map[string]string `json:"parser_identities"`
	Coverage                coverageJSON      `json:"coverage"`
	RecordCount             int               `json:"record_count"`
	PostingCount            int               `json:"posting_count"`
	SourceBindingDigest     string            `json:"source_binding_digest"`
	PayloadDigest           string            `json:"payload_digest"`
	IndexIdentity           string            `json:"index_identity"`
	GenerationIdentity      string            `json:"generation_identity"`
	SemanticDigest          string            `json:"semantic_digest"`
}

type bindingJSON struct {
	RepositoryIdentity      string `json:"repository_identity"`
	WorktreeIdentity        string `json:"worktree_identity"`
	CommittedHead           string `json:"committed_head"`
	DirtyOverlayFingerprint string `json:"dirty_overlay_fingerprint"`
}

type coverageJSON struct {
	PathCoverage             float64        `json:"path_coverage"`
	LanguageCoverage         float64        `json:"language_coverage"`
	IndexedPathCount         int            `json:"indexed_path_count"`
	ExcludedPathCount        int            `json:"excluded_path_count"`
	UnsupportedLanguageCount int            `json:"unsupported_language_count"`
	ParseFailureCount        int            `json:"parse_failure_count"`
	ExclusionReasonCounts    map[string]int `json:"exclusion_reason_counts"`
}

func encodeManifest(manifest model.Manifest) ([]byte, error) {
	return encodeManifestContext(context.Background(), manifest, nil)
}

func encodeManifestContext(ctx context.Context, manifest model.Manifest, observed func(buildPhase)) ([]byte, error) {
	if !manifestVariableBounds(manifest) {
		return nil, ErrInvalidManifest
	}
	if err := observeBuildContext(ctx, observed, buildPhaseManifest); err != nil {
		return nil, err
	}
	if err := validateManifestContext(ctx, manifest, observed); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(manifestToJSON(manifest))
	if err != nil || len(encoded) == 0 || len(encoded) > maximumManifestBytes {
		return nil, ErrInvalidManifest
	}
	// Manifest maps contain at most 64 bounded keys/values, so json.Marshal's
	// canonical map ordering is a fixed-size pass. Check immediately afterward
	// before any digest or state mutation.
	if err := observeBuildContext(ctx, observed, buildPhaseManifest); err != nil {
		return nil, err
	}
	return encoded, nil
}

func decodeManifest(encoded []byte) (model.Manifest, error) {
	if len(encoded) == 0 || len(encoded) > maximumManifestBytes || !utf8.Valid(encoded) {
		return model.Manifest{}, ErrInvalidManifest
	}
	decoder := json.NewDecoder(bytes.NewReader(encoded))
	decoder.DisallowUnknownFields()
	var value manifestJSON
	if err := decoder.Decode(&value); err != nil {
		return model.Manifest{}, ErrInvalidManifest
	}
	if token, err := decoder.Token(); err != io.EOF || token != nil {
		return model.Manifest{}, ErrInvalidManifest
	}
	manifest := manifestFromJSON(value)
	canonical, err := encodeManifest(manifest)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return model.Manifest{}, ErrInvalidManifest
	}
	return manifest, nil
}

func validateManifest(manifest model.Manifest) error {
	return validateManifestContext(context.Background(), manifest, nil)
}

func validateManifestContext(ctx context.Context, manifest model.Manifest, observed func(buildPhase)) error {
	if !manifestVariableBounds(manifest) {
		return ErrInvalidManifest
	}
	limits := policy.ProductionLimits()
	coverage := manifest.Coverage
	validEngine, err := validTextContext(ctx, manifest.EngineVersion, 128, false, observed)
	if err != nil {
		return err
	}
	if manifest.FormatVersion != "2" || !validEngine || pathLike(manifest.EngineVersion) ||
		!validSHA256IdentityString(manifest.Binding.RepositoryIdentity) || !validSHA256IdentityString(manifest.Binding.WorktreeIdentity) ||
		!validObjectIdentityString(manifest.Binding.CommittedHead) || !validSHA256IdentityString(manifest.Binding.DirtyOverlayFingerprint) ||
		!validSHA256IdentityString(manifest.InclusionPolicyIdentity) || !validSHA256IdentityString(manifest.ExclusionPolicyIdentity) ||
		manifest.ParserIdentities == nil || len(manifest.ParserIdentities) > limits.MaximumCollectionItems ||
		coverage.ExclusionReasonCounts == nil || len(coverage.ExclusionReasonCounts) > limits.MaximumCollectionItems ||
		!finiteFraction(coverage.PathCoverage) || !finiteFraction(coverage.LanguageCoverage) ||
		!validCounter(coverage.IndexedPathCount) || !validCounter(coverage.ExcludedPathCount) || !validCounter(coverage.UnsupportedLanguageCount) || !validCounter(coverage.ParseFailureCount) ||
		manifest.RecordCount < 0 || manifest.RecordCount > maximumIndexRecords || manifest.PostingCount < 0 || manifest.PostingCount > maximumPostingTerms ||
		!validSHA256IdentityString(manifest.SourceBindingDigest) || !validSHA256IdentityString(manifest.PayloadDigest) ||
		manifest.PayloadDigest != manifest.IndexIdentity || !validSHA256IdentityString(manifest.GenerationIdentity) || !validSHA256IdentityString(manifest.SemanticDigest) {
		return ErrInvalidManifest
	}
	for key, value := range manifest.ParserIdentities {
		validValue, validationErr := validTextContext(ctx, value, 512, false, observed)
		if validationErr != nil {
			return validationErr
		}
		if !canonicalName.MatchString(key) || !validValue || pathLike(value) {
			return ErrInvalidManifest
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	for key, value := range coverage.ExclusionReasonCounts {
		if !canonicalName.MatchString(key) || !validCounter(value) {
			return ErrInvalidManifest
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	return ctx.Err()
}

func manifestVariableBounds(manifest model.Manifest) bool {
	limits := policy.ProductionLimits()
	if len(manifest.FormatVersion) > 2 || len(manifest.EngineVersion) > 128 ||
		len(manifest.Binding.RepositoryIdentity) > 71 || len(manifest.Binding.WorktreeIdentity) > 71 || len(manifest.Binding.CommittedHead) > 64 || len(manifest.Binding.DirtyOverlayFingerprint) > 71 ||
		len(manifest.InclusionPolicyIdentity) > 71 || len(manifest.ExclusionPolicyIdentity) > 71 ||
		len(manifest.SourceBindingDigest) > 71 || len(manifest.PayloadDigest) > 71 || len(manifest.IndexIdentity) > 71 || len(manifest.GenerationIdentity) > 71 || len(manifest.SemanticDigest) > 71 ||
		len(manifest.ParserIdentities) > limits.MaximumCollectionItems || len(manifest.Coverage.ExclusionReasonCounts) > limits.MaximumCollectionItems {
		return false
	}
	for key, value := range manifest.ParserIdentities {
		if len(key) > 128 || len(value) > 512 {
			return false
		}
	}
	for key := range manifest.Coverage.ExclusionReasonCounts {
		if len(key) > 128 {
			return false
		}
	}
	return true
}

func validObjectIdentityString(value string) bool {
	if len(value) != 40 && len(value) != 64 {
		return false
	}
	for index := range len(value) {
		character := value[index]
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func validCounter(value int) bool { return value >= 0 && int64(value) <= math.MaxInt32 }

func finiteFraction(value float64) bool {
	return value >= 0 && value <= 1 && !math.IsNaN(value) && !math.IsInf(value, 0) && !(value == 0 && math.Signbit(value))
}

func pathLike(value string) bool {
	return strings.HasPrefix(value, "/") || strings.Contains(value, `\`) || (len(value) >= 2 && value[1] == ':')
}

func manifestToJSON(manifest model.Manifest) manifestJSON {
	return manifestJSON{
		FormatVersion: manifest.FormatVersion, EngineVersion: manifest.EngineVersion,
		Binding: bindingJSON{
			RepositoryIdentity: manifest.Binding.RepositoryIdentity, WorktreeIdentity: manifest.Binding.WorktreeIdentity,
			CommittedHead: manifest.Binding.CommittedHead, DirtyOverlayFingerprint: manifest.Binding.DirtyOverlayFingerprint,
		},
		InclusionPolicyIdentity: manifest.InclusionPolicyIdentity, ExclusionPolicyIdentity: manifest.ExclusionPolicyIdentity,
		ParserIdentities: cloneStringMap(manifest.ParserIdentities),
		Coverage: coverageJSON{
			PathCoverage: manifest.Coverage.PathCoverage, LanguageCoverage: manifest.Coverage.LanguageCoverage,
			IndexedPathCount: manifest.Coverage.IndexedPathCount, ExcludedPathCount: manifest.Coverage.ExcludedPathCount,
			UnsupportedLanguageCount: manifest.Coverage.UnsupportedLanguageCount, ParseFailureCount: manifest.Coverage.ParseFailureCount,
			ExclusionReasonCounts: cloneIntMap(manifest.Coverage.ExclusionReasonCounts),
		},
		RecordCount: manifest.RecordCount, PostingCount: manifest.PostingCount, SourceBindingDigest: manifest.SourceBindingDigest,
		PayloadDigest: manifest.PayloadDigest, IndexIdentity: manifest.IndexIdentity, GenerationIdentity: manifest.GenerationIdentity,
		SemanticDigest: manifest.SemanticDigest,
	}
}

func manifestFromJSON(value manifestJSON) model.Manifest {
	return model.Manifest{
		FormatVersion: value.FormatVersion, EngineVersion: value.EngineVersion,
		Binding: model.Binding{
			RepositoryIdentity: value.Binding.RepositoryIdentity, WorktreeIdentity: value.Binding.WorktreeIdentity,
			CommittedHead: value.Binding.CommittedHead, DirtyOverlayFingerprint: value.Binding.DirtyOverlayFingerprint,
		},
		InclusionPolicyIdentity: value.InclusionPolicyIdentity, ExclusionPolicyIdentity: value.ExclusionPolicyIdentity,
		ParserIdentities: cloneStringMap(value.ParserIdentities),
		Coverage: model.Coverage{
			PathCoverage: value.Coverage.PathCoverage, LanguageCoverage: value.Coverage.LanguageCoverage,
			IndexedPathCount: value.Coverage.IndexedPathCount, ExcludedPathCount: value.Coverage.ExcludedPathCount,
			UnsupportedLanguageCount: value.Coverage.UnsupportedLanguageCount, ParseFailureCount: value.Coverage.ParseFailureCount,
			ExclusionReasonCounts: cloneIntMap(value.Coverage.ExclusionReasonCounts),
		},
		RecordCount: value.RecordCount, PostingCount: value.PostingCount, SourceBindingDigest: value.SourceBindingDigest,
		PayloadDigest: value.PayloadDigest, IndexIdentity: value.IndexIdentity, GenerationIdentity: value.GenerationIdentity,
		SemanticDigest: value.SemanticDigest,
	}
}

func cloneStringMap(input map[string]string) map[string]string {
	if input == nil {
		return nil
	}
	output := make(map[string]string, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}

func cloneIntMap(input map[string]int) map[string]int {
	if input == nil {
		return nil
	}
	output := make(map[string]int, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}

func sha256ID(value []byte) string {
	identity, _ := sha256IDContext(context.Background(), value, nil, buildPhasePayloadDigest)
	return identity
}

func sha256IDContext(ctx context.Context, value []byte, observed func(buildPhase), phase buildPhase) (string, error) {
	return sha256MaterialIDContext(ctx, nil, value, observed, phase)
}

func sha256MaterialIDContext(ctx context.Context, prefix, value []byte, observed func(buildPhase), phase buildPhase) (string, error) {
	if err := observeBuildContext(ctx, observed, phase); err != nil {
		return "", err
	}
	hasher := sha256.New()
	if len(prefix) != 0 {
		_, _ = hasher.Write(prefix)
	}
	const digestChunkBytes = 64 << 10
	for offset := 0; offset < len(value); offset += digestChunkBytes {
		end := min(len(value), offset+digestChunkBytes)
		_, _ = hasher.Write(value[offset:end])
		if err := observeBuildContext(ctx, observed, phase); err != nil {
			return "", err
		}
	}
	digest := hasher.Sum(nil)
	return "sha256:" + hex.EncodeToString(digest), ctx.Err()
}
