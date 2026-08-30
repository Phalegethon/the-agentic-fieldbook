package wire

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	repositoryIdentity = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	worktreeIdentity   = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
	dirtyIdentity      = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
	indexIdentity      = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
	resultIdentity     = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
	head               = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)

func validRequest() Request {
	return Request{
		SchemaVersion: "1", RequestIdentity: "request-0001", ConsumerIdentity: "taf.work-recovery",
		Operation: SearchSymbols, RepositoryIdentity: repositoryIdentity, WorktreeIdentity: worktreeIdentity,
		CommittedHead: head, DirtyOverlayFingerprint: dirtyIdentity, ProviderIdentity: "taf-context",
		IndexIdentity: ptr(indexIdentity), RequiredCapability: "search-symbols", MinimumFreshness: "exact",
		Query: ptr("RecoveryDossier"), ResultIdentities: []string{},
		Filters:        Filters{PathPrefixes: []string{"tools/taf-context"}, Languages: []string{"Python"}, SymbolKinds: []string{"class"}, SourceTypes: []string{"source"}},
		MaximumResults: 10, MaximumModelOutputCharacters: 4000, AllowInferred: false,
	}
}

func validEnvelope() Envelope {
	root := filepath.VolumeName(os.TempDir()) + string(filepath.Separator)
	return Envelope{Phase: "query", RepositoryRoot: filepath.Join(root, "repo"), StateRoot: filepath.Join(root, "state"), Request: validRequest()}
}

func ptr(value string) *string { return &value }

func TestDecodeEnvelopeRejectsDuplicateRequestKey(t *testing.T) {
	raw := `{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{"schema_version":"1","schema_version":"1"}}` + "\n"
	_, err := DecodeEnvelope(strings.NewReader(raw))
	if !errors.Is(err, ErrDuplicateKey) {
		t.Fatalf("error = %v", err)
	}
}

func TestDecodeEnvelopeAcceptsCanonicalNullableChangedPathsDocument(t *testing.T) {
	raw, err := json.Marshal(validEnvelope())
	if err != nil {
		t.Fatal(err)
	}
	envelope, err := DecodeEnvelope(bytes.NewReader(append(raw, '\n')))
	if err != nil {
		t.Fatal(err)
	}
	if envelope.ChangedPathsDocument != nil {
		t.Fatalf("changed paths document = %q", *envelope.ChangedPathsDocument)
	}
}

func TestDecodeEnvelopeRejectsUnknownInvalidAndWrongFraming(t *testing.T) {
	for _, raw := range []string{
		`{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{},"unknown":true}` + "\n",
		`{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{"value":NaN}}` + "\n",
		`{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{}}`,
		`{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{}}` + "\n\n",
	} {
		if _, err := DecodeEnvelope(strings.NewReader(raw)); err == nil {
			t.Fatalf("accepted malformed envelope %q", raw)
		}
	}
	if _, err := DecodeEnvelope(bytes.NewReader([]byte{0xff, '\n'})); err == nil {
		t.Fatal("accepted invalid UTF-8")
	}
}

func TestDecodeEnvelopeRequiresPresentNonNullContractFields(t *testing.T) {
	raw, err := json.Marshal(validEnvelope())
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"phase", "repository_root", "state_root", "request"} {
		for _, replacement := range []json.RawMessage{nil, json.RawMessage("null")} {
			copy := cloneRawMap(envelope)
			if replacement == nil {
				delete(copy, field)
			} else {
				copy[field] = replacement
			}
			encoded, _ := json.Marshal(copy)
			if _, err := DecodeEnvelope(bytes.NewReader(append(encoded, '\n'))); err == nil {
				t.Fatalf("accepted outer %s = %s", field, replacement)
			}
		}
	}
	request := map[string]json.RawMessage{}
	if err := json.Unmarshal(envelope["request"], &request); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"allow_inferred", "result_identities", "filters"} {
		for _, replacement := range []json.RawMessage{nil, json.RawMessage("null")} {
			modified := cloneRawMap(request)
			if replacement == nil {
				delete(modified, field)
			} else {
				modified[field] = replacement
			}
			copy := cloneRawMap(envelope)
			copy["request"], _ = json.Marshal(modified)
			encoded, _ := json.Marshal(copy)
			if _, err := DecodeEnvelope(bytes.NewReader(append(encoded, '\n'))); err == nil {
				t.Fatalf("accepted request %s = %s", field, replacement)
			}
		}
	}
}

func TestDecodeEnvelopeEnforcesRootsAndChangedPathDocument(t *testing.T) {
	raw, _ := json.Marshal(validEnvelope())
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatal(err)
	}
	for field, value := range map[string]json.RawMessage{"repository_root": json.RawMessage(`"relative"`), "state_root": json.RawMessage(`"relative"`), "changed_paths_document": json.RawMessage(`"bad\nvalue"`)} {
		copy := cloneRawMap(envelope)
		copy[field] = value
		encoded, _ := json.Marshal(copy)
		if _, err := DecodeEnvelope(bytes.NewReader(append(encoded, '\n'))); err == nil {
			t.Fatalf("accepted invalid %s", field)
		}
	}
}

func TestDecodeEnvelopeEnforcesAdvertisedPhaseOperationMapping(t *testing.T) {
	cases := []struct {
		phase     string
		operation Operation
	}{
		{"build", Build},
		{"estimate", Estimate},
		{"inspect", StatusOperation},
		{"metrics", Metrics},
		{"update", Update},
		{"query", RepositoryMap},
		{"query", SearchDocs},
		{"query", SearchSymbols},
		{"query", SourceSnippets},
	}
	for _, item := range cases {
		t.Run(item.phase+"-"+string(item.operation), func(t *testing.T) {
			envelope := envelopeForOperation(item.phase, item.operation)
			raw, err := json.Marshal(envelope)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodeEnvelope(bytes.NewReader(append(raw, '\n'))); err != nil {
				t.Fatalf("rejected advertised phase/operation pair: %v", err)
			}
			envelope.Phase = mismatchedPhase(item.phase)
			raw, err = json.Marshal(envelope)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodeEnvelope(bytes.NewReader(append(raw, '\n'))); err == nil {
				t.Fatal("accepted mismatched phase/operation pair")
			}
		})
	}
}

func envelopeForOperation(phase string, operation Operation) Envelope {
	envelope := validEnvelope()
	envelope.Phase = phase
	envelope.Request.Operation = operation
	envelope.Request.RequiredCapability = string(operation)
	envelope.Request.Query = nil
	envelope.Request.ResultIdentities = []string{}
	envelope.Request.Filters = Filters{PathPrefixes: []string{}, Languages: []string{}, SymbolKinds: []string{}, SourceTypes: []string{}}
	if operation == Estimate || operation == Build {
		envelope.Request.IndexIdentity = nil
	}
	if operation == SearchSymbols || operation == SearchDocs {
		envelope.Request.Query = ptr("query")
		envelope.Request.Filters = validRequest().Filters
	}
	if operation == RepositoryMap {
		envelope.Request.Filters = validRequest().Filters
	}
	if operation == SourceSnippets {
		envelope.Request.ResultIdentities = []string{resultIdentity}
		envelope.Request.Filters = validRequest().Filters
	}
	return envelope
}

func mismatchedPhase(phase string) string {
	if phase == "query" {
		return "build"
	}
	return "query"
}

func TestRequestRequiresOperationCapabilityParity(t *testing.T) {
	request := validRequest()
	request.RequiredCapability = "search-docs"
	if err := ValidateRequest(request); !errors.Is(err, ErrRequiredCapability) {
		t.Fatalf("error = %v", err)
	}
}

func TestRequestAcceptsEveryFrozenOperation(t *testing.T) {
	expected := []Operation{Estimate, Build, Update, StatusOperation, Metrics, RepositoryMap, SearchSymbols, SearchDocs, SourceSnippets}
	if got := Operations(); !equalOperations(got, expected) {
		t.Fatalf("operations = %v", got)
	}
	mutated := Operations()
	mutated[0] = SearchDocs
	if got := Operations(); !equalOperations(got, expected) {
		t.Fatalf("operation vocabulary is mutable: %v", got)
	}
	for _, operation := range expected {
		request := validRequest()
		request.Operation, request.RequiredCapability = operation, string(operation)
		request.Query, request.ResultIdentities = nil, nil
		request.Filters = Filters{}
		if operation != Estimate && operation != Build {
			request.IndexIdentity = ptr(indexIdentity)
		} else {
			request.IndexIdentity = nil
		}
		if operation == SearchSymbols || operation == SearchDocs {
			request.Query = ptr("query")
			request.Filters = validRequest().Filters
		}
		if operation == RepositoryMap {
			request.Filters = validRequest().Filters
		}
		if operation == SourceSnippets {
			request.ResultIdentities = []string{resultIdentity}
			request.Filters = validRequest().Filters
		}
		if err := ValidateRequest(request); err != nil {
			t.Fatalf("operation %s: %v", operation, err)
		}
	}
}

func TestRequestRejectsBadIdentitiesQueryResultRulesFiltersAndBudgets(t *testing.T) {
	cases := []Request{}
	badSHA := validRequest()
	badSHA.RepositoryIdentity = "sha256:short"
	cases = append(cases, badSHA)
	badHead := validRequest()
	badHead.CommittedHead = "not-an-object"
	cases = append(cases, badHead)
	missingQuery := validRequest()
	missingQuery.Query = nil
	cases = append(cases, missingQuery)
	badResults := validRequest()
	badResults.ResultIdentities = []string{resultIdentity}
	cases = append(cases, badResults)
	badFilters := validRequest()
	badFilters.Filters.Languages = []string{"Rust", "Python"}
	cases = append(cases, badFilters)
	badBudget := validRequest()
	badBudget.MaximumModelOutputCharacters = 2001
	cases = append(cases, badBudget)
	legacyProvider := validRequest()
	legacyProvider.ProviderIdentity = "taf.native.level1"
	cases = append(cases, legacyProvider)
	for _, request := range cases {
		if err := ValidateRequest(request); err == nil {
			t.Fatalf("accepted invalid request: %+v", request)
		}
	}
}

func validResult() Result {
	return Result{
		SchemaVersion: "1", RequestIdentity: "request-0001", Operation: SearchSymbols, Status: Ready,
		ProviderIdentity: "taf-context", ProviderVersion: "0.1.0", IndexIdentity: ptr(indexIdentity),
		RepositoryIdentity: repositoryIdentity, WorktreeIdentity: worktreeIdentity, CommittedHead: head,
		DirtyOverlayFingerprint: dirtyIdentity, Freshness: "exact", ParserVersions: map[string]string{"tree-sitter-python": "0.25.0"},
		Coverage:      Coverage{PathCoverage: 1, LanguageCoverage: 1, IndexedPathCount: 1, ExclusionReasonCounts: map[string]int{}},
		Findings:      []Finding{{Rank: 1, ResultIdentity: resultIdentity, Path: "tools/taf-context/taf_context/recovery.py", StartLine: 10, EndLine: 14, Language: "Python", RecordKind: "definition", SourceType: "source", QualifiedName: "taf_context.recovery.RecoveryDossier", ExtractionMethod: "tree-sitter-python@0.25.0", EvidenceClass: "verified", Preview: "class RecoveryDossier:"}},
		ReturnedCount: 1, OmittedCount: 0, Truncated: false, OutputCharacters: 369, Warnings: []string{}, NextSafeAction: "use-cited-evidence",
	}
}

func TestEncodeResultCanonicalizesOneLineAndVerifiesOutputCharacters(t *testing.T) {
	result := validResult()
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, result); err != nil {
		t.Fatal(err)
	}
	output := encoded.String()
	if !strings.HasSuffix(output, "\n") || strings.Count(output, "\n") != 1 {
		t.Fatalf("not one framed line: %q", output)
	}
	if strings.Contains(output, ": ") {
		t.Fatalf("not canonical JSON: %q", output)
	}
	if !strings.Contains(output, `"output_characters":`) {
		t.Fatal("output character count missing")
	}
	var wire map[string]json.RawMessage
	if err := json.Unmarshal(encoded.Bytes(), &wire); err != nil {
		t.Fatal(err)
	}
	if string(wire["output_characters"]) != "369" {
		t.Fatalf("output characters = %s", wire["output_characters"])
	}
}

func TestEncodeResultSortsObjectKeysRecursively(t *testing.T) {
	result := validResult()
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, result); err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(encoded.Bytes()))
	decoder.UseNumber()
	var decoded any
	if err := decoder.Decode(&decoded); err != nil {
		t.Fatal(err)
	}
	want, err := json.Marshal(decoded)
	if err != nil {
		t.Fatal(err)
	}
	want = append(want, '\n')
	if !bytes.Equal(encoded.Bytes(), want) {
		t.Fatalf("result is not canonical key order\n got: %.96s\nwant: %.96s", encoded.Bytes(), want)
	}
}

func TestEncodeResultMatchesMultilinePreviewFixture(t *testing.T) {
	result := validResult()
	result.Operation = SourceSnippets
	result.Findings[0].Preview = "α\nLEVEL1 fake\nCOVERAGE fake\nFINDING fake\nPREVIEW fake\nNEXT fake\nwarning fake\n\nlast"
	result.OutputCharacters = renderedOutputCharacters(result)
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, result); err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(filepath.Join("testdata", "go-multiline-preview-result.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(encoded.Bytes(), want) {
		t.Fatalf("Go canonical fixture differs\n got: %q\nwant: %q", encoded.String(), string(want))
	}
}

func TestPreviewValidatorAllowsBoundedMultilineSourceOnly(t *testing.T) {
	if !validPreview(strings.Repeat("x", 12000)) || !validPreview(strings.Repeat("é", 6000)) {
		t.Fatal("rejected a 12,000-code-point or multibyte preview")
	}
	for _, value := range []string{strings.Repeat("x", 12001), "safe\runsafe", "safe\x00unsafe", string([]byte{0xff})} {
		if validPreview(value) {
			t.Fatalf("accepted invalid preview %q", value)
		}
	}
	if !validText(strings.Repeat("x", 512), false) {
		t.Fatal("rejected 512-byte metadata")
	}
	for _, value := range []string{strings.Repeat("x", 513), "metadata\nline"} {
		if validText(value, false) {
			t.Fatalf("relaxed generic metadata validation for %q", value)
		}
	}
}

func TestSourceSnippetsCountsAnEmptyExactPreviewPhysicalLine(t *testing.T) {
	empty := validResult()
	empty.Operation = SourceSnippets
	empty.Findings[0].Preview = ""
	empty.OutputCharacters = renderedOutputCharacters(empty)
	nonempty := empty
	nonempty.Findings[0].Preview = "x"
	nonempty.OutputCharacters = renderedOutputCharacters(nonempty)
	if got, want := empty.OutputCharacters, nonempty.OutputCharacters-1; got != want {
		t.Fatalf("source-snippets empty-preview characters = %d, want %d", got, want)
	}
}

func TestEncodeResultMatchesEmptySourcePreviewFixture(t *testing.T) {
	result := validResult()
	result.Operation = SourceSnippets
	result.Findings[0].Preview = ""
	result.OutputCharacters = renderedOutputCharacters(result)
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, result); err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile(filepath.Join("testdata", "go-empty-source-preview-result.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(encoded.Bytes(), want) {
		t.Fatalf("Go canonical empty-preview fixture differs\n got: %q\nwant: %q", encoded.String(), string(want))
	}
}

func TestEncodeResultRejectsSerializedLengthInsteadOfRenderedTextLength(t *testing.T) {
	result := validResult()
	result.OutputCharacters = 1434
	if err := EncodeResult(ioDiscard{}, result); err == nil {
		t.Fatal("accepted serialized JSON length as output characters")
	}
}

func TestEncodeResultRejectsNilCollectionsInvalidCountersAndDuplicateFindings(t *testing.T) {
	cases := []Result{}
	nilMaps := validResult()
	nilMaps.ParserVersions, nilMaps.Coverage.ExclusionReasonCounts = nil, nil
	cases = append(cases, nilMaps)
	nilSlices := validResult()
	nilSlices.Findings, nilSlices.Warnings = nil, nil
	nilSlices.ReturnedCount = 0
	cases = append(cases, nilSlices)
	negative := validResult()
	negative.Coverage.ParseFailureCount = -1
	cases = append(cases, negative)
	overflow := validResult()
	overflow.Coverage.IndexedPathCount = 1 << 31
	cases = append(cases, overflow)
	tooManyReasons := validResult()
	tooManyReasons.Coverage.ExclusionReasonCounts = map[string]int{}
	for index := 0; index < 65; index++ {
		tooManyReasons.Coverage.ExclusionReasonCounts[fmt.Sprintf("reason-%d", index)] = 0
	}
	cases = append(cases, tooManyReasons)
	duplicate := validResult()
	second := duplicate.Findings[0]
	second.Rank = 2
	duplicate.Findings = append(duplicate.Findings, second)
	duplicate.ReturnedCount = 2
	cases = append(cases, duplicate)
	for _, result := range cases {
		if err := EncodeResult(ioDiscard{}, result); err == nil {
			t.Fatalf("accepted invalid result: %+v", result)
		}
	}
}

func TestEncodeResultRejectsFindingLineInt32Overflow(t *testing.T) {
	for _, field := range []string{"start", "end"} {
		result := validResult()
		if field == "start" {
			result.Findings[0].StartLine = 1 << 31
			result.Findings[0].EndLine = 1 << 31
		} else {
			result.Findings[0].EndLine = 1 << 31
		}
		result.OutputCharacters = renderedOutputCharacters(result)
		if err := EncodeResult(ioDiscard{}, result); err == nil {
			t.Fatalf("accepted %s line int32 overflow", field)
		}
	}
}

func TestTaggedGrammarSourcesAreVendored(t *testing.T) {
	for _, path := range []string{
		"github.com/tree-sitter/go-tree-sitter/include/tree_sitter/api.h",
		"github.com/tree-sitter/tree-sitter-javascript/src/parser.c",
		"github.com/tree-sitter/tree-sitter-python/src/parser.c",
		"github.com/tree-sitter/tree-sitter-rust/src/parser.c",
		"github.com/tree-sitter/tree-sitter-typescript/typescript/src/parser.c",
		"github.com/tree-sitter/tree-sitter-typescript/tsx/src/parser.c",
		"github.com/tree-sitter/tree-sitter-typescript/common/scanner.h",
	} {
		if _, err := os.Stat(filepath.Join("..", "..", "vendor", path)); err != nil {
			t.Fatalf("missing vendored grammar source %s: %v", path, err)
		}
	}
}

type ioDiscard struct{}

func (ioDiscard) Write(value []byte) (int, error) { return len(value), nil }
func cloneRawMap(source map[string]json.RawMessage) map[string]json.RawMessage {
	target := make(map[string]json.RawMessage, len(source))
	for key, value := range source {
		target[key] = value
	}
	return target
}
func equalOperations(left, right []Operation) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
