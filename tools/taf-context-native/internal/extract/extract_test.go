package extract

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"reflect"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

func TestGoExtractorUsesASTRangesAndAliases(t *testing.T) {
	file := stableFile("internal/api/service.go", `package api
import (
	cfg "example/config"
	"fmt"
	"gopkg.in/yaml.v3"
)
type Service struct {
	Value string
}
type Runner interface {
	Run()
}
func Helper() {}
func (s *Service) Run() {}
`)
	records, report := NewRegistry().Extract(file)
	if report.ParserVersion != "go/parser@go1.27" || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	assertRecord(t, records, recordExpectation{"api", model.Module, "source", 1, 1, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"cfg", model.Import, "source", 3, 3, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"fmt", model.Import, "source", 4, 4, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"gopkg.in/yaml.v3", model.Import, "source", 5, 5, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"api.Service", model.Definition, "source", 7, 9, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"api.Runner", model.Definition, "source", 10, 12, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"api.Helper", model.Definition, "source", 13, 13, "go", "go/parser@go1.27", model.Verified})
	assertRecord(t, records, recordExpectation{"api.Service.Run", model.Definition, "source", 14, 14, "go", "go/parser@go1.27", model.Verified})
	if len(records) != 8 {
		t.Fatalf("record count = %d, want 8: %#v", len(records), records)
	}
}

func TestGoExtractorClassifiesMainAndBoundsSyntaxErrors(t *testing.T) {
	records, report := NewRegistry().Extract(stableFile("cmd/tool/main.go", "package main\nfunc main() {\n}\n"))
	assertRecord(t, records, recordExpectation{"main.main", model.EntryPoint, "source", 2, 3, "go", "go/parser@go1.27", model.Verified})
	if report.ParseFailures != 0 {
		t.Fatalf("main report = %#v", report)
	}

	records, report = NewRegistry().Extract(stableFile("broken.go", "package broken\nfunc (\n"))
	if len(records) != 0 || report.ParserVersion != "go/parser@go1.27" || report.ParseFailures != 1 || !contains(report.WarningCodes, "go-parse-failure") || len(report.WarningCodes) > 64 {
		t.Fatalf("syntax-error extraction = records %#v report %#v", records, report)
	}
}

func TestGoExtractorRequiresExactMainEntryPointSignature(t *testing.T) {
	fixtures := []string{
		"package main\nfunc main(value int) {}\n",
		"package main\nfunc main() int { return 0 }\n",
		"package main\nfunc main[T any]() {}\n",
	}
	for index, source := range fixtures {
		records, report := NewRegistry().Extract(stableFile(fmt.Sprintf("cmd/invalid-%d.go", index), source))
		if report.ParseFailures != 0 {
			t.Fatalf("fixture %d report = %#v", index, report)
		}
		assertRecord(t, records, recordExpectation{"main.main", model.Definition, "source", 2, 2, "go", "go/parser@go1.27", model.Verified})
		if entries := recordsOfKind(records, model.EntryPoint); len(entries) != 0 {
			t.Fatalf("fixture %d promoted invalid main signature: %#v", index, entries)
		}
	}
}

func TestMarkdownExtractorIgnoresFencedHeadingsAndAlignsSections(t *testing.T) {
	file := stableFile("docs/guide.md", "# Guide\nintro\n```go\n# Not Heading\n```\nInstallation\n------------\ndetails\n### API ###\ntext\n")
	records, report := NewRegistry().Extract(file)
	if report.ParserVersion != "taf-markdown@1" || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	assertRecord(t, records, recordExpectation{"Guide", model.Heading, "document", 1, 1, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"Guide.Installation", model.Heading, "document", 6, 7, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"Guide.Installation.API", model.Heading, "document", 9, 9, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"Guide#chunk-1", model.DocumentChunk, "document", 1, 2, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"Guide.Installation#chunk-1", model.DocumentChunk, "document", 6, 8, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"Guide.Installation.API#chunk-1", model.DocumentChunk, "document", 9, 10, "markdown", "taf-markdown@1", model.Verified})
	for _, record := range records {
		if strings.Contains(record.QualifiedName, "Not Heading") {
			t.Fatalf("fenced heading became a record: %#v", record)
		}
		if record.RecordKind == model.DocumentChunk && record.StartLine <= 5 && record.EndLine >= 3 {
			t.Fatalf("document chunk overlaps fenced code: %#v", record)
		}
	}
}

func TestMarkdownExtractorBoundsLongSectionChunks(t *testing.T) {
	var source strings.Builder
	source.WriteString("# Long\n")
	for line := 0; line < 450; line++ {
		source.WriteString("body\n")
	}
	records, report := NewRegistry().Extract(stableFile("docs/long.md", source.String()))
	if report.ParseFailures != 0 {
		t.Fatalf("report = %#v", report)
	}
	chunks := recordsOfKind(records, model.DocumentChunk)
	wantRanges := [][2]int{{1, 200}, {201, 400}, {401, 451}}
	if len(chunks) != len(wantRanges) {
		t.Fatalf("chunks = %#v, want %d", chunks, len(wantRanges))
	}
	for index, want := range wantRanges {
		if chunks[index].StartLine != want[0] || chunks[index].EndLine != want[1] || chunks[index].EndLine-chunks[index].StartLine+1 > maximumMarkdownChunkLines {
			t.Fatalf("chunk %d = %#v, want range %v", index, chunks[index], want)
		}
	}
}

func TestMarkdownExtractorDoesNotPromoteIndentedCodeToSetextHeading(t *testing.T) {
	records, report := NewRegistry().Extract(stableFile("docs/code.md", "    not a heading\n---\n"))
	if report.ParseFailures != 0 {
		t.Fatalf("report = %#v", report)
	}
	if headings := recordsOfKind(records, model.Heading); len(headings) != 0 {
		t.Fatalf("indented code became a setext heading: %#v", headings)
	}
}

func TestMarkdownExtractorRejectsBlockSetextCandidatesAndParsesEmptyATX(t *testing.T) {
	source := "- list item\n---\n***\n---\n# ###\n# named ###\n# literal###\n"
	records, report := NewRegistry().Extract(stableFile("docs/commonmark.md", source))
	if report.ParseFailures != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, forbidden := range []string{"- list item", "***", "###"} {
		for _, record := range recordsOfKind(records, model.Heading) {
			if record.QualifiedName == forbidden {
				t.Fatalf("block/closing marker became heading %q: %#v", forbidden, records)
			}
		}
	}
	assertRecord(t, records, recordExpectation{"named", model.Heading, "document", 6, 6, "markdown", "taf-markdown@1", model.Verified})
	assertRecord(t, records, recordExpectation{"literal###", model.Heading, "document", 7, 7, "markdown", "taf-markdown@1", model.Verified})
}

func TestMarkdownExtractorBoundsLegalRepeatedHeadingMemory(t *testing.T) {
	const maximumMeasuredAllocations = 96 << 20
	maximum := int(NewRegistry().byExtension[".md"].MaximumBytes())
	line := []byte("# heading\n")
	source := bytes.Repeat(line, maximum/len(line))
	file := stableFileBytes("docs/repeated.md", source)
	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	records, report := NewRegistry().Extract(file)
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	allocated := after.TotalAlloc - before.TotalAlloc
	if allocated > maximumMeasuredAllocations {
		t.Fatalf("repeated-heading extraction allocated %d bytes, ceiling = %d", allocated, maximumMeasuredAllocations)
	}
	if len(records) > maximumMarkdownRecords || report.ParseFailures != 1 || !contains(report.WarningCodes, "markdown-record-limit") {
		t.Fatalf("repeated-heading result = %d records, report %#v", len(records), report)
	}
}

func TestMarkdownExtractorOmitsLineBeyondChunkByteBound(t *testing.T) {
	source := "# Long\n" + strings.Repeat("x", maximumMarkdownChunkBytes+1) + "\n"
	records, report := NewRegistry().Extract(stableFile("docs/wide.md", source))
	if !contains(report.WarningCodes, "markdown-line-too-long") || report.ParseFailures != 0 {
		t.Fatalf("wide-line report = %#v", report)
	}
	for _, record := range recordsOfKind(records, model.DocumentChunk) {
		if record.StartLine <= 2 && record.EndLine >= 2 {
			t.Fatalf("chunk includes an over-budget line: %#v", record)
		}
	}
}

func TestTOMLExtractorIndexesTablesArraysAndDottedKeysWithoutValues(t *testing.T) {
	file := stableFile("config/service.toml", `title = "TOML Example"
[database]
server = "192.0.2.1"
ports = [8000, 8001]
[servers.alpha]
ip = "198.51.100.1"
[[products]]
name = "Hammer"
"quoted.key".value = 7
`)
	records, report := NewRegistry().Extract(file)
	if report.ParserVersion != "taf-toml@1" || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, expected := range []recordExpectation{
		{"title", model.Configuration, "configuration", 1, 1, "toml", "taf-toml@1", model.Verified},
		{"database", model.Configuration, "configuration", 2, 2, "toml", "taf-toml@1", model.Verified},
		{"database.server", model.Configuration, "configuration", 3, 3, "toml", "taf-toml@1", model.Verified},
		{"database.ports", model.Configuration, "configuration", 4, 4, "toml", "taf-toml@1", model.Verified},
		{"servers.alpha", model.Configuration, "configuration", 5, 5, "toml", "taf-toml@1", model.Verified},
		{"servers.alpha.ip", model.Configuration, "configuration", 6, 6, "toml", "taf-toml@1", model.Verified},
		{"products", model.Configuration, "configuration", 7, 7, "toml", "taf-toml@1", model.Verified},
		{"products.name", model.Configuration, "configuration", 8, 8, "toml", "taf-toml@1", model.Verified},
		{"products.quoted.key.value", model.Configuration, "configuration", 9, 9, "toml", "taf-toml@1", model.Verified},
	} {
		assertRecord(t, records, expected)
	}
	assertNoStoredValues(t, records, "TOML Example", "192.0.2.1", "8000", "198.51.100.1", "Hammer")
}

func TestJSONExtractorIndexesNestedAndArrayKeysWithoutValues(t *testing.T) {
	file := stableFile("config/service.json", "{\n  \"service\": {\n    \"host\": \"192.0.2.1\",\n    \"ports\": [8000, 8001],\n    \"workers\": [{\"name\": \"alpha\"}, {\"name\": \"beta\"}]\n  },\n  \"enabled\": true\n}\n")
	records, report := NewRegistry().Extract(file)
	if report.ParserVersion != "encoding/json@go1.27" || report.ParseFailures != 0 || len(report.WarningCodes) != 0 {
		t.Fatalf("report = %#v", report)
	}
	for _, expected := range []recordExpectation{
		{"service", model.Configuration, "configuration", 2, 2, "json", "encoding/json@go1.27", model.Verified},
		{"service.host", model.Configuration, "configuration", 3, 3, "json", "encoding/json@go1.27", model.Verified},
		{"service.ports", model.Configuration, "configuration", 4, 4, "json", "encoding/json@go1.27", model.Verified},
		{"service.workers", model.Configuration, "configuration", 5, 5, "json", "encoding/json@go1.27", model.Verified},
		{"service.workers[].name", model.Configuration, "configuration", 5, 5, "json", "encoding/json@go1.27", model.Verified},
		{"enabled", model.Configuration, "configuration", 7, 7, "json", "encoding/json@go1.27", model.Verified},
	} {
		assertRecord(t, records, expected)
	}
	if countQualified(records, "service.workers[].name") != 2 {
		t.Fatalf("array-object keys = %#v, want two name records", records)
	}
	seenIdentities := map[string]bool{}
	for _, record := range records {
		if seenIdentities[record.Identity] {
			t.Fatalf("duplicate JSON record identity: %#v", record)
		}
		seenIdentities[record.Identity] = true
	}
	assertNoStoredValues(t, records, "192.0.2.1", "8000", "alpha", "beta", "true")
}

func TestConfigurationExtractorsBoundDepthCollectionsAndMalformedInput(t *testing.T) {
	deep := strings.Repeat(`{"nested":`, maximumJSONDepth+1) + "0" + strings.Repeat("}", maximumJSONDepth+1)
	records, report := NewRegistry().Extract(stableFile("config/deep.json", deep))
	if report.ParseFailures != 1 || !contains(report.WarningCodes, "json-depth-limit") || len(records) > maximumJSONDepth {
		t.Fatalf("deep JSON = %d records, report %#v", len(records), report)
	}

	var wide strings.Builder
	wide.WriteByte('{')
	for index := 0; index <= maximumConfigurationCollectionItems; index++ {
		if index != 0 {
			wide.WriteByte(',')
		}
		fmt.Fprintf(&wide, "%q:0", fmt.Sprintf("key-%02d", index))
	}
	wide.WriteByte('}')
	records, report = NewRegistry().Extract(stableFile("config/wide.json", wide.String()))
	if report.ParseFailures != 1 || !contains(report.WarningCodes, "json-collection-limit") || len(records) > maximumConfigurationCollectionItems {
		t.Fatalf("wide JSON = %d records, report %#v", len(records), report)
	}

	for _, fixture := range []boundary.StableFile{
		stableFile("config/broken.json", `{"key":`),
		stableFile("config/broken.toml", "[missing\nkey = 1\n"),
		stableFile("config/unsupported-escape.toml", `"bad\x61" = 1`),
	} {
		records, report = NewRegistry().Extract(fixture)
		if len(records) != 0 || report.ParseFailures != 1 || len(report.WarningCodes) != 1 || len(report.WarningCodes) > 64 {
			t.Fatalf("malformed %s = records %#v report %#v", fixture.RelativePath, records, report)
		}
	}
}

func TestTOMLExtractorFailsClosedOnMalformedDuplicateAndConflictingInput(t *testing.T) {
	fixtures := map[string]string{
		"invalid-value":         "valid = 1\ninvalid = ?\n",
		"duplicate-key":         "key = 1\nkey = 2\n",
		"duplicate-table":       "[table]\nkey = 1\n[table]\nother = 2\n",
		"value-table-conflict":  "conflict = 1\n[conflict]\n",
		"table-value-conflict":  "[conflict]\nchild = 1\n[conflict.child]\n",
		"unsupported-multiline": "value = \"\"\"unsupported\"\"\"\n",
		"invalid-bare-key":      "bad?key = 1\n",
	}
	for name, source := range fixtures {
		t.Run(name, func(t *testing.T) {
			records, report := NewRegistry().Extract(stableFile("config/invalid.toml", source))
			if len(records) != 0 || report.ParseFailures != 1 || !contains(report.WarningCodes, "toml-parse-failure") {
				t.Fatalf("records = %#v, report = %#v", records, report)
			}
		})
	}
}

func TestConfigurationExtractorOmitsUnrepresentableQualifiedName(t *testing.T) {
	records, report := NewRegistry().Extract(stableFile("config/control.json", `{"line\nbreak": 1}`))
	if len(records) != 0 || report.ParseFailures != 0 || !contains(report.WarningCodes, "invalid-extractor-record") {
		t.Fatalf("control-character key = records %#v report %#v", records, report)
	}
}

func TestRegistryUsesStableExactMetadataAndRecordIdentities(t *testing.T) {
	file := stableFile("pkg/value.go", "package pkg\ntype Value struct{}\n")
	first, firstReport := NewRegistry().Extract(file)
	second, secondReport := NewRegistry().Extract(file)
	if !reflect.DeepEqual(first, second) || !reflect.DeepEqual(firstReport, secondReport) {
		t.Fatalf("repeated extraction differs:\n%#v %#v\n%#v %#v", first, firstReport, second, secondReport)
	}
	identityPattern := regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	seen := map[string]bool{}
	for _, record := range first {
		if !identityPattern.MatchString(record.Identity) || seen[record.Identity] {
			t.Fatalf("record identity is invalid or duplicated: %#v", record)
		}
		seen[record.Identity] = true
		if record.Path != file.RelativePath || record.SourceDigest != "sha256:"+file.SHA256 {
			t.Fatalf("record source binding = %#v", record)
		}
	}
	other, _ := NewRegistry().Extract(stableFile("other/value.go", string(file.Bytes)))
	if first[0].Identity == other[0].Identity {
		t.Fatal("record identity did not bind the repository-relative path")
	}

	unsupported, report := NewRegistry().Extract(stableFile("config/value.yaml", "key: value\n"))
	if len(unsupported) != 0 || report.ParseFailures != 0 || !contains(report.WarningCodes, "unsupported-language") {
		t.Fatalf("unsupported extension = records %#v report %#v", unsupported, report)
	}
	mdx, report := NewRegistry().Extract(stableFile("docs/value.mdx", "# Value\n"))
	if len(mdx) == 0 || report.ParserVersion != "taf-markdown@1" {
		t.Fatalf("registered MDX metadata was not consumed: records %#v report %#v", mdx, report)
	}
}

func TestRegistryRejectsNonCanonicalRepositoryRelativePaths(t *testing.T) {
	tooDeep := strings.Repeat("d/", 256) + "value.go"
	tooLong := strings.Repeat("a", 4094) + ".go"
	invalid := []string{
		"/absolute.go",
		"//server/share.go",
		"C:/windows.go",
		`C:\windows.go`,
		"relative\\windows.go",
		"./value.go",
		"a/../value.go",
		"a//value.go",
		"a/value.go/",
		".",
		"..",
		"nul\x00value.go",
		"bad-\xff.go",
		tooDeep,
		tooLong,
	}
	for _, relative := range invalid {
		records, report := NewRegistry().Extract(stableFile(relative, "package invalid\n"))
		if len(records) != 0 || report.ParseFailures != 1 || !contains(report.WarningCodes, "invalid-stable-file") {
			t.Fatalf("path %q accepted: records %#v report %#v", relative, records, report)
		}
	}
}

func TestRecordIdentitiesAreIndependentOfExtractorRecordOrder(t *testing.T) {
	file := stableFile("pkg/permutation.go", "package pkg\n")
	extractor := goExtractor{extensions: []string{".go"}}
	alpha := structuralRecord("pkg.Duplicate", model.Definition, "source", 1, 1)
	alpha.SearchTerms = []string{"alpha"}
	beta := structuralRecord("pkg.Duplicate", model.Definition, "source", 1, 1)
	beta.SearchTerms = []string{"beta"}
	forward, invalidForward := finalizeRecords(file, extractor, []model.Record{alpha, beta})
	reverse, invalidReverse := finalizeRecords(file, extractor, []model.Record{beta, alpha})
	if invalidForward || invalidReverse || !reflect.DeepEqual(forward, reverse) {
		t.Fatalf("identity permutation differs:\nforward=%#v\nreverse=%#v", forward, reverse)
	}
	wantIdentities := []string{
		"sha256:5172cb8c26da13b5edc9d99563ce557c88bac2ee1ae36e81d5ca9e4b716a7e32",
		"sha256:c7f85bb6b2a1915bfed971dcd29f5227e3d94c6958d7994c2c95aa933e4c7527",
	}
	for index, want := range wantIdentities {
		if forward[index].Identity != want {
			t.Fatalf("identity %d = %q, want exact %q", index, forward[index].Identity, want)
		}
	}
}

type recordExpectation struct {
	qualifiedName string
	kind          model.RecordKind
	sourceType    string
	startLine     int
	endLine       int
	language      string
	method        string
	evidence      model.EvidenceClass
}

func stableFile(relative, contents string) boundary.StableFile {
	return stableFileBytes(relative, []byte(contents))
}

func stableFileBytes(relative string, contents []byte) boundary.StableFile {
	digest := sha256.Sum256(contents)
	return boundary.StableFile{RelativePath: relative, Bytes: contents, SHA256: fmt.Sprintf("%x", digest), Size: int64(len(contents))}
}

func assertRecord(t *testing.T, records []model.Record, want recordExpectation) {
	t.Helper()
	for _, record := range records {
		if record.QualifiedName == want.qualifiedName && record.RecordKind == want.kind && record.StartLine == want.startLine && record.EndLine == want.endLine {
			if record.SourceType != want.sourceType || record.Language != want.language || record.ExtractionMethod != want.method || record.EvidenceClass != want.evidence {
				t.Fatalf("record = %#v, want %#v", record, want)
			}
			return
		}
	}
	t.Fatalf("record %#v missing from %#v", want, records)
}

func recordsOfKind(records []model.Record, kind model.RecordKind) []model.Record {
	var selected []model.Record
	for _, record := range records {
		if record.RecordKind == kind {
			selected = append(selected, record)
		}
	}
	return selected
}

func countQualified(records []model.Record, qualified string) int {
	count := 0
	for _, record := range records {
		if record.QualifiedName == qualified {
			count++
		}
	}
	return count
}

func assertNoStoredValues(t *testing.T, records []model.Record, values ...string) {
	t.Helper()
	for _, record := range records {
		if record.Preview != "" {
			t.Fatalf("configuration preview stores a value: %#v", record)
		}
		joinedTerms := strings.Join(record.SearchTerms, "\x00")
		for _, value := range values {
			if strings.Contains(record.QualifiedName, value) || strings.Contains(joinedTerms, value) {
				t.Fatalf("configuration record stores value %q: %#v", value, record)
			}
		}
	}
}

func contains(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}
