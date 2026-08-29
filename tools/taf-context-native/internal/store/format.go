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
	"path"
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
	indexFormatVersion            uint16 = 1
	maximumDecompressedIndexBytes        = 64 << 20
	maximumEncodedIndexBytes             = 64 << 20
	maximumManifestBytes                 = 256 << 10
	maximumIndexStringBytes              = 4096
	maximumIndexRecords                  = 1_000_000
	maximumPostingTerms                  = 1_000_000
	maximumPostingOrdinals               = 8_000_000
	maximumTermsPerRecord                = 64
)

func encodeIndex(input []model.Record) ([]byte, error) {
	if len(input) > maximumIndexRecords {
		return nil, ErrInvalidIndex
	}
	records := make([]model.Record, len(input))
	totalOrdinals := 0
	for index, record := range input {
		normalized, err := normalizeRecord(record)
		if err != nil {
			return nil, err
		}
		if totalOrdinals > maximumPostingOrdinals-len(normalized.SearchTerms) {
			return nil, ErrInvalidIndex
		}
		totalOrdinals += len(normalized.SearchTerms)
		records[index] = normalized
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Identity < records[j].Identity })
	for index := 1; index < len(records); index++ {
		if records[index-1].Identity == records[index].Identity {
			return nil, ErrInvalidIndex
		}
	}
	postings, terms, err := buildPostings(records)
	if err != nil {
		return nil, err
	}

	plainSize, err := encodedPlainSize(records, terms, postings)
	if err != nil {
		return nil, err
	}
	var plain bytes.Buffer
	plain.Grow(plainSize)
	plain.Write(indexMagic)
	writeUint16(&plain, indexFormatVersion)
	writeUint32(&plain, uint32(len(records)))
	for _, record := range records {
		writeRecord(&plain, record)
	}
	writeUint32(&plain, uint32(len(terms)))
	for _, term := range terms {
		writeString(&plain, term)
		ordinals := postings[term]
		writeUint32(&plain, uint32(len(ordinals)))
		for _, ordinal := range ordinals {
			writeUint32(&plain, ordinal)
		}
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
	decoder := binaryDecoder{value: plain}
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
	records := make([]model.Record, recordCount)
	totalOrdinals := 0
	for index := range records {
		record, err := decoder.readRecord()
		if err != nil || (index > 0 && records[index-1].Identity >= record.Identity) {
			return nil, nil, ErrInvalidIndex
		}
		if totalOrdinals > maximumPostingOrdinals-len(record.SearchTerms) {
			return nil, nil, ErrInvalidIndex
		}
		totalOrdinals += len(record.SearchTerms)
		records[index] = record
	}
	postingCount, err := decoder.readCount(maximumPostingTerms)
	if err != nil || !decoder.canContain(postingCount, 8) {
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
		ordinals := make([]uint32, ordinalCount)
		for ordinalIndex := range ordinals {
			ordinal, err := decoder.readUint32()
			if err != nil || uint64(ordinal) >= uint64(len(records)) || (ordinalIndex > 0 && ordinals[ordinalIndex-1] >= ordinal) {
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
	expected, terms, err := buildPostings(records)
	if err != nil || len(terms) != len(postings) {
		return nil, nil, ErrInvalidIndex
	}
	for _, term := range terms {
		if !slices.Equal(expected[term], postings[term]) {
			return nil, nil, ErrInvalidIndex
		}
	}
	return records, postings, nil
}

func normalizeRecord(record model.Record) (model.Record, error) {
	record.SearchTerms = slices.Clone(record.SearchTerms)
	if len(record.SearchTerms) > maximumTermsPerRecord {
		return model.Record{}, ErrInvalidIndex
	}
	for _, term := range record.SearchTerms {
		if !validTerm(term) {
			return model.Record{}, ErrInvalidIndex
		}
	}
	sort.Strings(record.SearchTerms)
	for index := 1; index < len(record.SearchTerms); index++ {
		if record.SearchTerms[index-1] == record.SearchTerms[index] {
			return model.Record{}, ErrInvalidIndex
		}
	}
	if !validRecord(record) {
		return model.Record{}, ErrInvalidIndex
	}
	return record, nil
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
	if !validText(value, maximumIndexStringBytes, false) || strings.Contains(value, `\`) || strings.Contains(value, ":") || strings.HasPrefix(value, "/") || path.Clean(value) != value {
		return false
	}
	for _, component := range strings.Split(value, "/") {
		if component == "" || component == "." || component == ".." {
			return false
		}
	}
	return true
}

func validText(value string, maximum int, empty bool) bool {
	return utf8.ValidString(value) && len(value) <= maximum && (empty || value != "") &&
		strings.IndexFunc(value, unicode.IsControl) < 0
}

func buildPostings(records []model.Record) (map[string][]uint32, []string, error) {
	postings := make(map[string][]uint32)
	total := 0
	for ordinal, record := range records {
		if ordinal > math.MaxUint32 {
			return nil, nil, ErrInvalidIndex
		}
		for _, term := range record.SearchTerms {
			if total == maximumPostingOrdinals {
				return nil, nil, ErrInvalidIndex
			}
			postings[term] = append(postings[term], uint32(ordinal))
			total++
		}
	}
	if len(postings) > maximumPostingTerms {
		return nil, nil, ErrInvalidIndex
	}
	terms := make([]string, 0, len(postings))
	for term := range postings {
		terms = append(terms, term)
	}
	sort.Strings(terms)
	return postings, terms, nil
}

func encodedPlainSize(records []model.Record, terms []string, postings map[string][]uint32) (int, error) {
	size := len(indexMagic) + 2 + 4 + 4
	add := func(amount int) bool {
		if amount < 0 || size > maximumDecompressedIndexBytes-amount {
			return false
		}
		size += amount
		return true
	}
	for _, record := range records {
		values := []string{record.Identity, record.Path, record.Language, string(record.RecordKind), record.SourceType, record.QualifiedName, record.ExtractionMethod, string(record.EvidenceClass), record.SourceDigest, record.Preview}
		for _, value := range values {
			if !add(4 + len(value)) {
				return 0, ErrInvalidIndex
			}
		}
		if !add(12 + 4*len(record.SearchTerms)) {
			return 0, ErrInvalidIndex
		}
		for _, term := range record.SearchTerms {
			if !add(len(term)) {
				return 0, ErrInvalidIndex
			}
		}
	}
	for _, term := range terms {
		if !add(8 + len(term) + 4*len(postings[term])) {
			return 0, ErrInvalidIndex
		}
	}
	return size, nil
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
	if err != nil || !utf8.Valid(raw) {
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
