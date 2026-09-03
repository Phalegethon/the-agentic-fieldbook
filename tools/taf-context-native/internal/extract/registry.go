// Package extract turns already-opened stable source into deterministic
// structural records. Extractors never reopen repository paths.
package extract

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"path"
	"slices"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

const maximumExtractorWarnings = 64

const (
	maximumStableRelativePathBytes      = 4096
	maximumStableRelativePathComponents = 256
)

var ErrInvalidExtractor = errors.New("extractor metadata does not match the inventory registry")

type Extractor interface {
	Language() string
	ParserVersion() string
	Extensions() []string
	MaximumBytes() int64
	Extract(boundary.StableFile) ([]model.Record, Report)
}

type Report struct {
	ParserVersion string
	ParseFailures int
	WarningCodes  []string
}

// Incomplete reports whether this extraction omitted potential records. Keep
// this vocabulary exhaustive: adding a limiting/failure warning requires an
// explicit decision here, so it cannot silently become complete evidence.
func (report Report) Incomplete() bool {
	if report.ParseFailures != 0 {
		return true
	}
	for _, warning := range report.WarningCodes {
		incomplete, known := extractorWarningCompleteness[warning]
		if !known || incomplete {
			return true
		}
	}
	return false
}

// Every emitted warning must be listed here. Unknown vocabulary fails closed.
var extractorWarningCompleteness = map[string]bool{
	"go-record-limit": true, "go-parse-failure": true, "go-unsupported-receiver": false,
	"markdown-invalid-utf8": true, "markdown-unterminated-fence": false, "markdown-heading-limit": true, "markdown-line-too-long": true, "markdown-record-limit": true,
	"invalid-stable-file": true, "invalid-extractor-record": true, "extractor-panic": true, "warning-limit": true,
	"tree-sitter-cancelled": true, "tree-sitter-capture-limit": true, "tree-sitter-depth-limit": true, "tree-sitter-import-limit": true, "tree-sitter-match-limit": true, "tree-sitter-invalid-range": true, "tree-sitter-record-limit": true,
	"json-depth-limit": true, "json-collection-limit": true, "json-record-limit": true, "toml-record-limit": true, "toml-parse-failure": true,
	"javascript-generated-name": false, "javascript-dynamic-lookup": false, "typescript-generated-name": false, "typescript-dynamic-lookup": false,
	"python-generated-name": false, "python-dynamic-lookup": false, "rust-generated-name": false,
	"reference-limit": true, "reference-skipped": false,
	"json-parse-failure": true, "unsupported-language": true,
	"python-parse-failure": true, "python-query-failure": true, "python-syntax-error": true,
	"javascript-parse-failure": true, "javascript-query-failure": true, "javascript-syntax-error": true,
	"typescript-parse-failure": true, "typescript-query-failure": true, "typescript-syntax-error": true,
	"rust-parse-failure": true, "rust-query-failure": true, "rust-syntax-error": true,
	"parse-failure": true,
}

// parseFailureCodes are the warning codes an extractor emits together with
// ParseFailures == 1. boundedReport guarantees that every parse failure
// carries one of them (adding the generic "parse-failure" otherwise), so a
// report can be reconstructed from the codes the source catalog persists.
var parseFailureCodes = map[string]bool{
	"parse-failure": true, "invalid-stable-file": true, "extractor-panic": true,
	"go-parse-failure": true, "json-parse-failure": true, "toml-parse-failure": true,
	"markdown-invalid-utf8": true, "markdown-unterminated-fence": true, "markdown-heading-limit": true, "markdown-record-limit": true,
	"python-parse-failure": true, "python-syntax-error": true,
	"javascript-parse-failure": true, "javascript-syntax-error": true,
	"typescript-parse-failure": true, "typescript-syntax-error": true,
	"rust-parse-failure": true, "rust-syntax-error": true,
}

// ReportFromCodes rebuilds the completeness view of a persisted extraction
// report from its warning codes. It is exact because boundedReport guarantees
// a parse-failure code for every parse failure.
func ReportFromCodes(codes []string) Report {
	report := Report{}
	if len(codes) != 0 {
		report.WarningCodes = append([]string(nil), codes...)
	}
	for _, code := range codes {
		if parseFailureCodes[code] {
			report.ParseFailures = 1
			break
		}
	}
	return report
}

func hasParseFailureCode(codes []string) bool {
	for _, code := range codes {
		if parseFailureCodes[code] {
			return true
		}
	}
	return false
}

// PolicyDescriptor binds registry/path validation ceilings that affect whether
// a source reaches an extractor. Inputs are constants owned by this package.
// v2: dynamic constructs and work limits no longer downgrade literal records;
// the identity change forces a rebuild of v1 indexes.
func PolicyDescriptor() string {
	return fmt.Sprintf("extract-v2 path=%d components=%d warnings=%d", maximumStableRelativePathBytes, maximumStableRelativePathComponents, maximumExtractorWarnings)
}

type Registry struct {
	byExtension map[string]Extractor
}

// NewRegistry installs only parsers whose extensions come from inventory's
// immutable language metadata. Later source parsers use Register against the
// same metadata surface.
func NewRegistry() Registry {
	registry := Registry{byExtension: make(map[string]Extractor)}
	for _, metadata := range inventory.ExtensionRegistry() {
		var extractor Extractor
		switch metadata.Language {
		case "go":
			extractor = goExtractor{extensions: metadata.Extensions}
		case "python":
			extractor = pythonExtractor{extensions: metadata.Extensions}
		case "javascript":
			extractor = javascriptExtractor{extensions: metadata.Extensions}
		case "typescript":
			extractor = typescriptExtractor{extensions: metadata.Extensions}
		case "rust":
			extractor = rustExtractor{extensions: metadata.Extensions}
		case "markdown":
			extractor = markdownExtractor{extensions: metadata.Extensions}
		case "json":
			extractor = jsonExtractor{extensions: metadata.Extensions}
		case "toml":
			extractor = tomlExtractor{extensions: metadata.Extensions}
		default:
			continue
		}
		_ = registry.Register(extractor)
	}
	return registry
}

func (registry *Registry) Register(extractor Extractor) error {
	if registry == nil || extractor == nil || extractor.Language() == "" || extractor.ParserVersion() == "" || extractor.MaximumBytes() <= 0 {
		return ErrInvalidExtractor
	}
	allowed := make(map[string]string)
	for _, metadata := range inventory.ExtensionRegistry() {
		for _, extension := range metadata.Extensions {
			allowed[extension] = metadata.Language
		}
	}
	if registry.byExtension == nil {
		registry.byExtension = make(map[string]Extractor)
	}
	extensions := extractor.Extensions()
	if len(extensions) == 0 {
		return ErrInvalidExtractor
	}
	seen := make(map[string]bool, len(extensions))
	for _, extension := range extensions {
		if extension == "" || extension != strings.ToLower(extension) || allowed[extension] != extractor.Language() {
			return ErrInvalidExtractor
		}
		if seen[extension] {
			return ErrInvalidExtractor
		}
		seen[extension] = true
		if existing := registry.byExtension[extension]; existing != nil && existing.Language() != extractor.Language() {
			return ErrInvalidExtractor
		}
	}
	for _, extension := range extensions {
		registry.byExtension[extension] = extractor
	}
	return nil
}

func (registry Registry) Extract(file boundary.StableFile) (records []model.Record, report Report) {
	return registry.ExtractContext(context.Background(), file)
}

// ParserIdentities returns a defensive copy of every installed production
// parser identity, including languages not observed in a particular build.
func (registry Registry) ParserIdentities() map[string]string {
	identities := make(map[string]string, len(registry.byExtension))
	for _, extractor := range registry.byExtension {
		identities[extractor.Language()] = extractor.ParserVersion()
	}
	return identities
}

type contextExtractor interface {
	ExtractContext(context.Context, boundary.StableFile) ([]model.Record, Report)
}

// ExtractContext preserves the Task 4 registry boundary while allowing native
// parsers to be cancelled by later engine operations. Non-native extractors
// retain their original bounded Extract implementation.
func (registry Registry) ExtractContext(ctx context.Context, file boundary.StableFile) (records []model.Record, report Report) {
	if !stableFileMatches(file) {
		return nil, boundedReport(Report{ParseFailures: 1, WarningCodes: []string{"invalid-stable-file"}})
	}
	extension := strings.ToLower(path.Ext(file.RelativePath))
	extractor := registry.byExtension[extension]
	if extractor == nil {
		return nil, boundedReport(Report{WarningCodes: []string{"unsupported-language"}})
	}
	report.ParserVersion = extractor.ParserVersion()
	defer func() {
		if recover() != nil {
			records = nil
			report = boundedReport(Report{ParserVersion: extractor.ParserVersion(), ParseFailures: 1, WarningCodes: []string{"extractor-panic"}})
		}
	}()
	if file.Size > extractor.MaximumBytes() {
		return nil, boundedReport(Report{ParserVersion: extractor.ParserVersion(), ParseFailures: 1, WarningCodes: []string{"invalid-stable-file"}})
	}
	if contextual, ok := extractor.(contextExtractor); ok {
		records, report = contextual.ExtractContext(ctx, file)
	} else {
		records, report = extractor.Extract(file)
	}
	report.ParserVersion = extractor.ParserVersion()
	report = boundedReport(report)
	records, invalid := finalizeRecords(file, extractor, records)
	if invalid {
		report.WarningCodes = append(report.WarningCodes, "invalid-extractor-record")
		report = boundedReport(report)
	}
	return records, report
}

func stableFileMatches(file boundary.StableFile) bool {
	if !canonicalStableRelativePath(file.RelativePath) || file.Size < 0 || file.Size != int64(len(file.Bytes)) || !utf8.Valid(file.Bytes) {
		return false
	}
	digest := sha256.Sum256(file.Bytes)
	want := hex.EncodeToString(digest[:])
	return file.SHA256 == want || file.SHA256 == "sha256:"+want
}

func canonicalStableRelativePath(relative string) bool {
	if relative == "" || len(relative) > maximumStableRelativePathBytes || !utf8.ValidString(relative) || strings.ContainsAny(relative, "\x00\\") || strings.HasPrefix(relative, "/") || path.Clean(relative) != relative {
		return false
	}
	if len(relative) >= 2 && ((relative[0] >= 'A' && relative[0] <= 'Z') || (relative[0] >= 'a' && relative[0] <= 'z')) && relative[1] == ':' {
		return false
	}
	components := strings.Split(relative, "/")
	if len(components) > maximumStableRelativePathComponents {
		return false
	}
	for _, component := range components {
		if component == "" || component == "." || component == ".." {
			return false
		}
	}
	return true
}

func finalizeRecords(file boundary.StableFile, extractor Extractor, input []model.Record) ([]model.Record, bool) {
	lineMaximum := sourceLineCount(file.Bytes)
	lineStarts := sourceLineStarts(file.Bytes)
	sourceDigest := file.SHA256
	if !strings.HasPrefix(sourceDigest, "sha256:") {
		sourceDigest = "sha256:" + sourceDigest
	}
	output := make([]model.Record, 0, len(input))
	invalid := false
	for _, record := range input {
		record.Path = file.RelativePath
		record.Language = extractor.Language()
		record.ExtractionMethod = extractor.ParserVersion()
		record.SourceDigest = sourceDigest
		if record.StartLine < 1 || record.EndLine < record.StartLine || record.EndLine > lineMaximum || record.QualifiedName == "" || len(record.QualifiedName) > 512 || !utf8.ValidString(record.QualifiedName) || strings.ContainsAny(record.QualifiedName, "\x00\n\r") || record.SourceType == "" || record.EvidenceClass == "" {
			invalid = true
			continue
		}
		if !consistentReferenceFields(record) {
			invalid = true
			continue
		}
		if record.RecordKind == model.Reference {
			// A reference is found by the names it refers to, not by the name
			// of the definition it sits in.
			record.SearchTerms = referenceSearchTerms(record.TargetName)
		} else {
			record.SearchTerms = normalizedSearchTerms(record.QualifiedName, record.SearchTerms)
		}
		record.Preview = previewFor(record, file.Bytes, lineStarts)
		output = append(output, record)
	}
	sort.Slice(output, func(i, j int) bool {
		left, right := output[i], output[j]
		if left.StartLine != right.StartLine {
			return left.StartLine < right.StartLine
		}
		if left.EndLine != right.EndLine {
			return left.EndLine < right.EndLine
		}
		if left.RecordKind != right.RecordKind {
			return left.RecordKind < right.RecordKind
		}
		if left.QualifiedName != right.QualifiedName {
			return left.QualifiedName < right.QualifiedName
		}
		if left.TargetName != right.TargetName {
			return left.TargetName < right.TargetName
		}
		if left.SourceType != right.SourceType {
			return left.SourceType < right.SourceType
		}
		if left.EvidenceClass != right.EvidenceClass {
			return left.EvidenceClass < right.EvidenceClass
		}
		if comparison := slices.Compare(left.SearchTerms, right.SearchTerms); comparison != 0 {
			return comparison < 0
		}
		return left.Preview < right.Preview
	})
	occurrences := make(map[string]int)
	for index := range output {
		semanticKey := recordIdentity(output[index], 0)
		occurrence := occurrences[semanticKey]
		occurrences[semanticKey] = occurrence + 1
		output[index].Identity = recordIdentity(output[index], occurrence)
	}
	sort.SliceStable(output, func(i, j int) bool {
		left, right := output[i], output[j]
		if left.StartLine != right.StartLine {
			return left.StartLine < right.StartLine
		}
		if left.EndLine != right.EndLine {
			return left.EndLine < right.EndLine
		}
		if left.RecordKind != right.RecordKind {
			return left.RecordKind < right.RecordKind
		}
		if left.QualifiedName != right.QualifiedName {
			return left.QualifiedName < right.QualifiedName
		}
		if left.TargetName != right.TargetName {
			return left.TargetName < right.TargetName
		}
		return left.Identity < right.Identity
	})
	return output, invalid
}

// consistentReferenceFields mirrors the store's format-4 rule: a reference
// record carries the target table of the definition it belongs to and counts
// exactly the occurrences that table declares, only an import record may
// otherwise name a target (the module specifier it binds), and no other kind
// carries either field.
func consistentReferenceFields(record model.Record) bool {
	if !utf8.ValidString(record.TargetName) || strings.ContainsAny(record.TargetName, "\x00\n\r") {
		return false
	}
	isReference := record.RecordKind == model.Reference
	if record.ReferenceCount < 0 || isReference != (record.ReferenceCount >= 1) {
		return false
	}
	if isReference {
		_, total, valid := model.ScanReferenceTable([]byte(record.TargetName))
		return valid && total == uint64(record.ReferenceCount)
	}
	return record.TargetName == "" || (len(record.TargetName) <= maximumTargetSpecifierBytes && record.RecordKind == model.Import)
}

// referenceSearchTerms are the lower-cased target names of a reference
// record's table, without sub-tokenization: a reference is found by the whole
// name it refers to, so the index keys of the record are the keys of its
// targets.
func referenceSearchTerms(table string) []string {
	entries, ok := model.ParseReferenceTable(table)
	if !ok {
		return nil
	}
	terms := make([]string, 0, len(entries))
	seen := make(map[string]bool, len(entries))
	for _, entry := range entries {
		term := strings.ToLower(strings.TrimSpace(entry.Name))
		if term == "" || len(term) > 128 || seen[term] {
			continue
		}
		seen[term] = true
		terms = append(terms, term)
	}
	sort.Strings(terms)
	return terms
}

func recordIdentity(record model.Record, occurrence int) string {
	hash := sha256.New()
	writeIdentityPart(hash, "taf-record-v1")
	writeIdentityPart(hash, record.Path)
	writeIdentityPart(hash, string(record.RecordKind))
	writeIdentityPart(hash, record.QualifiedName)
	writeIdentityPart(hash, fmt.Sprintf("%d", record.StartLine))
	writeIdentityPart(hash, fmt.Sprintf("%d", record.EndLine))
	writeIdentityPart(hash, record.Language)
	writeIdentityPart(hash, record.SourceType)
	writeIdentityPart(hash, record.ExtractionMethod)
	writeIdentityPart(hash, string(record.EvidenceClass))
	writeIdentityPart(hash, record.SourceDigest)
	// Only a record that actually names a target contributes the part, so the
	// identity of every record without one is exactly what it was in format 3.
	if record.TargetName != "" {
		writeIdentityPart(hash, record.TargetName)
	}
	writeIdentityPart(hash, fmt.Sprintf("%d", occurrence))
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

type identityWriter interface {
	Write([]byte) (int, error)
}

func writeIdentityPart(writer identityWriter, value string) {
	var size [8]byte
	binary.BigEndian.PutUint64(size[:], uint64(len(value)))
	_, _ = writer.Write(size[:])
	_, _ = writer.Write([]byte(value))
}

func normalizedSearchTerms(qualified string, additional []string) []string {
	candidates := append([]string{qualified}, additional...)
	for _, field := range strings.FieldsFunc(qualified, func(character rune) bool {
		return !unicode.IsLetter(character) && !unicode.IsDigit(character) && character != '_'
	}) {
		candidates = append(candidates, field)
	}
	seen := make(map[string]bool)
	terms := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		candidate = strings.ToLower(strings.TrimSpace(candidate))
		if candidate == "" || len(candidate) > 128 || !utf8.ValidString(candidate) || seen[candidate] {
			continue
		}
		seen[candidate] = true
		terms = append(terms, candidate)
		if len(terms) == 64 {
			break
		}
	}
	sort.Strings(terms)
	return terms
}

func sourceLineCount(source []byte) int {
	if len(source) == 0 {
		return 0
	}
	count := bytes.Count(source, []byte{'\n'}) + 1
	if source[len(source)-1] == '\n' {
		count--
	}
	return count
}

func boundedReport(report Report) Report {
	if report.ParseFailures < 0 || report.ParseFailures > 1 {
		report.ParseFailures = 1
	}
	if report.ParseFailures == 1 && !hasParseFailureCode(report.WarningCodes) {
		report.WarningCodes = append(report.WarningCodes, "parse-failure")
	}
	sort.Strings(report.WarningCodes)
	warnings := report.WarningCodes[:0]
	for _, warning := range report.WarningCodes {
		if warning == "" || len(warning) > 128 || (len(warnings) != 0 && warnings[len(warnings)-1] == warning) {
			continue
		}
		warnings = append(warnings, warning)
	}
	if len(warnings) > maximumExtractorWarnings {
		warnings = append(append([]string(nil), warnings[:maximumExtractorWarnings-1]...), "warning-limit")
		sort.Strings(warnings)
	}
	report.WarningCodes = warnings
	return report
}
