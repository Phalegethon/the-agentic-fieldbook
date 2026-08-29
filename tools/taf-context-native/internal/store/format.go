package store

import (
	"bytes"
	"compress/zlib"
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
	"sort"
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
	sha256Identity     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	objectIdentity     = regexp.MustCompile(`^(?:[0-9a-f]{40}|[0-9a-f]{64})$`)
	canonicalName      = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
)

const (
	indexFormatVersion                   uint16 = 1
	maximumDecompressedIndexBytes               = 64 << 20
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
	conservativeTermMemoryBytes                 = 32
	conservativeStringAllocationOverhead        = 16
)

func encodeIndex(input []model.Record) ([]byte, error) {
	preflight, err := preflightEncodeIndex(input)
	if err != nil {
		return nil, err
	}
	recordOrder := make([]uint32, len(input))
	for index := range recordOrder {
		recordOrder[index] = uint32(index)
	}
	sort.Slice(recordOrder, func(i, j int) bool {
		return input[recordOrder[i]].Identity < input[recordOrder[j]].Identity
	})
	for index := 1; index < len(recordOrder); index++ {
		if input[recordOrder[index-1]].Identity == input[recordOrder[index]].Identity {
			return nil, ErrInvalidIndex
		}
	}
	postingReferences := make([]postingReference, 0, preflight.totalTerms)
	for ordinal, inputIndex := range recordOrder {
		record := input[inputIndex]
		for termIndex := range record.SearchTerms {
			postingReferences = append(postingReferences, postingReference{recordOrdinal: uint32(ordinal), termIndex: uint8(termIndex)})
		}
	}
	postingTerm := func(reference postingReference) string {
		record := input[recordOrder[reference.recordOrdinal]]
		return record.SearchTerms[reference.termIndex]
	}
	sort.Slice(postingReferences, func(i, j int) bool {
		left, right := postingTerm(postingReferences[i]), postingTerm(postingReferences[j])
		if left != right {
			return left < right
		}
		return postingReferences[i].recordOrdinal < postingReferences[j].recordOrdinal
	})

	plainSize, postingCount, err := encodedCanonicalPlainSize(preflight.recordBytes, postingReferences, postingTerm)
	if err != nil {
		return nil, err
	}
	var plain bytes.Buffer
	plain.Grow(plainSize)
	plain.Write(indexMagic)
	writeUint16(&plain, indexFormatVersion)
	writeUint32(&plain, uint32(len(recordOrder)))
	for _, inputIndex := range recordOrder {
		writeCanonicalRecord(&plain, input[inputIndex])
	}
	writeUint32(&plain, uint32(postingCount))
	for start := 0; start < len(postingReferences); {
		term := postingTerm(postingReferences[start])
		end := start + 1
		for end < len(postingReferences) && postingTerm(postingReferences[end]) == term {
			end++
		}
		writeString(&plain, term)
		writeUint32(&plain, uint32(end-start))
		for _, reference := range postingReferences[start:end] {
			writeUint32(&plain, reference.recordOrdinal)
		}
		start = end
	}
	if plain.Len() != plainSize || plain.Len() > maximumDecompressedIndexBytes {
		return nil, ErrInvalidIndex
	}

	var encoded bytes.Buffer
	writer, err := zlib.NewWriterLevel(&encoded, zlib.BestCompression)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
	}
	if _, err := writer.Write(plain.Bytes()); err != nil {
		_ = writer.Close()
		return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
	}
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidIndex, err)
	}
	if encoded.Len() == 0 || encoded.Len() > maximumEncodedIndexBytes {
		return nil, ErrInvalidIndex
	}
	return encoded.Bytes(), nil
}

type postingReference struct {
	recordOrdinal uint32
	termIndex     uint8
}

func encodedCanonicalPlainSize(recordBytes int64, references []postingReference, term func(postingReference) string) (int, int, error) {
	total := recordBytes
	postingCount := 0
	for start := 0; start < len(references); {
		value := term(references[start])
		end := start + 1
		for end < len(references) && term(references[end]) == value {
			end++
		}
		if !addEncodeSize(&total, 8+int64(len(value))+4*int64(end-start), maximumDecompressedIndexBytes) {
			return 0, 0, ErrInvalidIndex
		}
		postingCount++
		if postingCount > maximumPostingTerms {
			return 0, 0, ErrInvalidIndex
		}
		start = end
	}
	if total < 0 || total > math.MaxInt {
		return 0, 0, ErrInvalidIndex
	}
	return int(total), postingCount, nil
}

type encodeIndexPreflight struct {
	recordBytes int64
	totalTerms  int
}

// preflightEncodeIndex rejects inputs that cannot fit the wire or conservative
// process-memory budget before encodeIndex allocates its compact index arrays.
func preflightEncodeIndex(input []model.Record) (encodeIndexPreflight, error) {
	if len(input) > maximumIndexRecords {
		return encodeIndexPreflight{}, ErrInvalidIndex
	}
	serialized := int64(len(indexMagic) + 2 + 4 + 4)
	totalTerms := 0
	for _, record := range input {
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
			if !addEncodeSize(&serialized, int64(len(term)), maximumDecompressedIndexBytes) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
		}
	}
	recordBytes := serialized
	// Every occurrence requires one posting ordinal even before accounting for
	// its term and group header. This lower bound is sufficient for early wire
	// rejection without constructing a postings representation.
	if !addEncodeSize(&serialized, 4*int64(totalTerms), maximumDecompressedIndexBytes) {
		return encodeIndexPreflight{}, ErrInvalidIndex
	}
	// The encoded bytes.Buffer may retain growth slack while the complete plain
	// buffer is still live, so charge twice the encoded ceiling.
	peak := int64(maximumDecompressedIndexBytes + 2*maximumEncodedIndexBytes + conservativeZlibWorkspaceBytes)
	for _, amount := range []int64{
		serialized,
		int64(len(input)) * conservativeRecordMemoryBytes,
		int64(totalTerms) * conservativeTermMemoryBytes,
	} {
		if amount < 0 || peak > maximumIndexPeakBytes-amount {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		peak += amount
	}
	for _, record := range input {
		if !validRecord(record) {
			return encodeIndexPreflight{}, ErrInvalidIndex
		}
		for termIndex, term := range record.SearchTerms {
			if !validTerm(term) {
				return encodeIndexPreflight{}, ErrInvalidIndex
			}
			for prior := 0; prior < termIndex; prior++ {
				if record.SearchTerms[prior] == term {
					return encodeIndexPreflight{}, ErrInvalidIndex
				}
			}
		}
	}
	return encodeIndexPreflight{recordBytes: recordBytes, totalTerms: totalTerms}, nil
}

func addEncodeSize(total *int64, amount, maximum int64) bool {
	if total == nil || amount < 0 || *total < 0 || amount > maximum-*total {
		return false
	}
	*total += amount
	return true
}

func decodeIndex(encoded []byte) ([]model.Record, map[string][]uint32, error) {
	if len(encoded) == 0 || len(encoded) > maximumEncodedIndexBytes {
		return nil, nil, ErrInvalidIndex
	}
	compressed := bytes.NewReader(encoded)
	reader, err := zlib.NewReader(compressed)
	if err != nil {
		return nil, nil, ErrInvalidIndex
	}
	plain, readErr := io.ReadAll(io.LimitReader(reader, maximumDecompressedIndexBytes+1))
	closeErr := reader.Close()
	if readErr != nil || closeErr != nil || len(plain) > maximumDecompressedIndexBytes || compressed.Len() != 0 {
		return nil, nil, ErrInvalidIndex
	}
	budget := decodeMemoryBudget{used: int64(cap(encoded) + cap(plain) + conservativeZlibWorkspaceBytes)}
	if budget.used < 0 || budget.used > maximumIndexPeakBytes {
		return nil, nil, ErrInvalidIndex
	}
	decoder := binaryDecoder{value: plain, budget: &budget}
	magic, err := decoder.readBytes(len(indexMagic))
	if err != nil || !bytes.Equal(magic, indexMagic) {
		return nil, nil, ErrInvalidIndex
	}
	version, err := decoder.readUint16()
	if err != nil || version != indexFormatVersion {
		return nil, nil, ErrInvalidIndex
	}
	recordCount, err := decoder.readCount(maximumIndexRecords)
	if err != nil || !decoder.canContain(recordCount, minimumEncodedRecordBytes()) {
		return nil, nil, ErrInvalidIndex
	}
	if budget.reserve(int64(recordCount)*conservativeRecordMemoryBytes) != nil {
		return nil, nil, ErrInvalidIndex
	}
	initialRecordCapacity := min(recordCount, 1024)
	records := make([]model.Record, 0, initialRecordCapacity)
	totalOrdinals := 0
	for index := 0; index < recordCount; index++ {
		record, err := decoder.readRecord()
		if err != nil || (index > 0 && records[index-1].Identity >= record.Identity) {
			return nil, nil, ErrInvalidIndex
		}
		if totalOrdinals > maximumPostingOrdinals-len(record.SearchTerms) {
			return nil, nil, ErrInvalidIndex
		}
		totalOrdinals += len(record.SearchTerms)
		records = append(records, record)
	}
	postingCount, err := decoder.readCount(maximumPostingTerms)
	if err != nil || !decoder.canContain(postingCount, 8) {
		return nil, nil, ErrInvalidIndex
	}
	if budget.reserve(int64(postingCount)*64) != nil {
		return nil, nil, ErrInvalidIndex
	}
	postings := make(map[string][]uint32, postingCount)
	previousTerm := ""
	decodedOrdinals := 0
	for index := 0; index < postingCount; index++ {
		term, err := decoder.readString(128)
		if err != nil || !validTerm(term) || (index > 0 && previousTerm >= term) {
			return nil, nil, ErrInvalidIndex
		}
		ordinalCount, err := decoder.readCount(len(records))
		if err != nil || ordinalCount == 0 || !decoder.canContain(ordinalCount, 4) || decodedOrdinals > maximumPostingOrdinals-ordinalCount {
			return nil, nil, ErrInvalidIndex
		}
		decodedOrdinals += ordinalCount
		if budget.reserve(int64(ordinalCount)*4) != nil {
			return nil, nil, ErrInvalidIndex
		}
		ordinals := make([]uint32, ordinalCount)
		for ordinalIndex := range ordinals {
			ordinal, err := decoder.readUint32()
			if err != nil || uint64(ordinal) >= uint64(len(records)) || (ordinalIndex > 0 && ordinals[ordinalIndex-1] >= ordinal) {
				return nil, nil, ErrInvalidIndex
			}
			if _, found := slices.BinarySearch(records[ordinal].SearchTerms, term); !found {
				return nil, nil, ErrInvalidIndex
			}
			ordinals[ordinalIndex] = ordinal
		}
		postings[term] = ordinals
		previousTerm = term
	}
	if decoder.remaining() != 0 || decodedOrdinals != totalOrdinals {
		return nil, nil, ErrInvalidIndex
	}
	return records, postings, nil
}

func validRecord(record model.Record) bool {
	return sha256Identity.MatchString(record.Identity) && validRelativePath(record.Path) &&
		record.StartLine >= 1 && record.EndLine >= record.StartLine && uint64(record.EndLine) <= math.MaxUint32 &&
		validText(record.Language, 128, false) && validRecordKind(record.RecordKind) && validSourceType(record.SourceType) &&
		validText(record.QualifiedName, 512, false) && validText(record.ExtractionMethod, 512, false) &&
		validEvidenceClass(record.EvidenceClass) && sha256Identity.MatchString(record.SourceDigest) &&
		validText(record.Preview, maximumIndexStringBytes, true)
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
	return validText(value, 128, false) && value == strings.TrimSpace(value) && value == strings.ToLower(value)
}

func validRelativePath(value string) bool {
	if !validText(value, maximumIndexStringBytes, false) || strings.Contains(value, `\`) || strings.Contains(value, ":") || strings.HasPrefix(value, "/") {
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

func validText(value string, maximum int, empty bool) bool {
	return utf8.ValidString(value) && len(value) <= maximum && (empty || value != "") &&
		strings.IndexFunc(value, unicode.IsControl) < 0
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
	if err := validateManifest(manifest); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(manifestToJSON(manifest))
	if err != nil || len(encoded) == 0 || len(encoded) > maximumManifestBytes {
		return nil, ErrInvalidManifest
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
	limits := policy.ProductionLimits()
	coverage := manifest.Coverage
	if manifest.FormatVersion != "1" || !validText(manifest.EngineVersion, 128, false) || pathLike(manifest.EngineVersion) ||
		!sha256Identity.MatchString(manifest.Binding.RepositoryIdentity) || !sha256Identity.MatchString(manifest.Binding.WorktreeIdentity) ||
		!objectIdentity.MatchString(manifest.Binding.CommittedHead) || !sha256Identity.MatchString(manifest.Binding.DirtyOverlayFingerprint) ||
		!sha256Identity.MatchString(manifest.InclusionPolicyIdentity) || !sha256Identity.MatchString(manifest.ExclusionPolicyIdentity) ||
		manifest.ParserIdentities == nil || len(manifest.ParserIdentities) > limits.MaximumCollectionItems ||
		coverage.ExclusionReasonCounts == nil || len(coverage.ExclusionReasonCounts) > limits.MaximumCollectionItems ||
		!finiteFraction(coverage.PathCoverage) || !finiteFraction(coverage.LanguageCoverage) ||
		!validCounter(coverage.IndexedPathCount) || !validCounter(coverage.ExcludedPathCount) || !validCounter(coverage.UnsupportedLanguageCount) || !validCounter(coverage.ParseFailureCount) ||
		manifest.RecordCount < 0 || manifest.RecordCount > maximumIndexRecords || manifest.PostingCount < 0 || manifest.PostingCount > maximumPostingTerms ||
		!sha256Identity.MatchString(manifest.SourceBindingDigest) || !sha256Identity.MatchString(manifest.PayloadDigest) ||
		manifest.PayloadDigest != manifest.IndexIdentity || !sha256Identity.MatchString(manifest.GenerationIdentity) || !sha256Identity.MatchString(manifest.SemanticDigest) {
		return ErrInvalidManifest
	}
	for key, value := range manifest.ParserIdentities {
		if !canonicalName.MatchString(key) || !validText(value, 512, false) || pathLike(value) {
			return ErrInvalidManifest
		}
	}
	for key, value := range coverage.ExclusionReasonCounts {
		if !canonicalName.MatchString(key) || !validCounter(value) {
			return ErrInvalidManifest
		}
	}
	return nil
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
	digest := sha256.Sum256(value)
	return "sha256:" + hex.EncodeToString(digest[:])
}
