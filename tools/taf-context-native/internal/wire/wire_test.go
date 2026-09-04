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

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const (
	repositoryIdentity = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	worktreeIdentity   = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
	dirtyIdentity      = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
	indexIdentity      = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
	resultIdentity     = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
	overviewIdentity   = "sha256:6666666666666666666666666666666666666666666666666666666666666666"
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
		{"query", RelatedSymbols},
		{"query", ChangedSymbols},
		{"query", RepositoryOverview},
	}
	for _, item := range cases {
		t.Run(item.phase+"-"+string(item.operation), func(t *testing.T) {
			envelope := envelopeForOperation(item.phase, item.operation)
			if _, err := DecodeEnvelope(bytes.NewReader(framedEnvelope(t, envelope))); err != nil {
				t.Fatalf("rejected advertised phase/operation pair: %v", err)
			}
			envelope.Phase = mismatchedPhase(item.phase)
			if _, err := DecodeEnvelope(bytes.NewReader(framedEnvelope(t, envelope))); err == nil {
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
	if operation == RelatedSymbols {
		envelope.Request.SchemaVersion = "2"
		envelope.Request.ResultIdentities = []string{resultIdentity}
		envelope.Request.Direction = ptr("callers")
		envelope.Request.Filters = validRequest().Filters
	}
	if operation == RepositoryOverview {
		envelope.Request.SchemaVersion = "4"
		envelope.Request.Filters = overviewFilters()
	}
	if operation == ChangedSymbols {
		envelope.Request.SchemaVersion = "3"
		envelope.Request.ChangedRanges = changedRanges(ChangedRange{Path: "internal/query/changed.go", Ranges: [][2]int{{1, 4}}})
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
	expected := []Operation{Estimate, Build, Update, StatusOperation, Metrics, RepositoryMap, SearchSymbols, SearchDocs, SourceSnippets, RelatedSymbols, ChangedSymbols, RepositoryOverview}
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
		if operation == RelatedSymbols {
			request.SchemaVersion = "2"
			request.ResultIdentities = []string{resultIdentity}
			request.Direction = ptr("callers")
			request.Filters = validRequest().Filters
		}
		if operation == ChangedSymbols {
			request.SchemaVersion = "3"
			request.ChangedRanges = changedRanges(ChangedRange{Path: "internal/query/changed.go", Ranges: [][2]int{{1, 4}}})
			request.Filters = validRequest().Filters
		}
		if operation == RepositoryOverview {
			request.SchemaVersion = "4"
			request.Filters = overviewFilters()
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

func TestResultValidationRequiresTruncatedWhenOmissionsAreCounted(t *testing.T) {
	rejected := validResult()
	rejected.OmittedCount, rejected.Truncated = 1, false
	if err := EncodeResult(ioDiscard{}, rejected); err == nil {
		t.Fatal("accepted a counted omission with truncated=false")
	}
	accepted := validResult()
	accepted.OmittedCount, accepted.Truncated = 0, true
	if err := EncodeResult(ioDiscard{}, accepted); err != nil {
		t.Fatalf("rejected an exhausted search reporting truncated=true with omitted_count=0: %v", err)
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

func relatedRequest() Request {
	request := validRequest()
	request.SchemaVersion = "2"
	request.Operation, request.RequiredCapability = RelatedSymbols, "related-symbols"
	request.Query = nil
	request.ResultIdentities = []string{resultIdentity}
	request.Direction = ptr("callers")
	return request
}

// envelopeWithRequestKeys re-encodes a framed envelope with the request keys
// replaced or, for a nil value, removed; schema-2 requests need the direction
// key spelled out even when it is null, which struct marshaling omits.
func envelopeWithRequestKeys(t *testing.T, envelope Envelope, overrides map[string]json.RawMessage) []byte {
	t.Helper()
	raw, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	var outer map[string]json.RawMessage
	if err := json.Unmarshal(raw, &outer); err != nil {
		t.Fatal(err)
	}
	var request map[string]json.RawMessage
	if err := json.Unmarshal(outer["request"], &request); err != nil {
		t.Fatal(err)
	}
	for key, value := range overrides {
		if value == nil {
			delete(request, key)
			continue
		}
		request[key] = value
	}
	if outer["request"], err = json.Marshal(request); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(outer)
	if err != nil {
		t.Fatal(err)
	}
	return append(encoded, '\n')
}

func TestDecodeEnvelopeRejectsDirectionUnderSchemaOne(t *testing.T) {
	envelope := validEnvelope()
	for _, direction := range []json.RawMessage{json.RawMessage(`"callers"`), json.RawMessage("null")} {
		raw := envelopeWithRequestKeys(t, envelope, map[string]json.RawMessage{"direction": direction})
		if _, err := DecodeEnvelope(bytes.NewReader(raw)); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-1 direction %s: error = %v", direction, err)
		}
	}
	request := validRequest()
	request.Direction = ptr("callers")
	if err := ValidateRequest(request); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("typed schema-1 direction: error = %v", err)
	}
}

func TestRequestRejectsRelatedSymbolsUnderSchemaOne(t *testing.T) {
	request := relatedRequest()
	request.SchemaVersion = "1"
	request.Direction = nil
	if err := ValidateRequest(request); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("error = %v", err)
	}
}

func TestDecodeEnvelopeAcceptsSchemaTwoRelatedSymbols(t *testing.T) {
	envelope := validEnvelope()
	envelope.Request = relatedRequest()
	raw, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeEnvelope(bytes.NewReader(append(raw, '\n')))
	if err != nil {
		t.Fatalf("rejected schema-2 related-symbols: %v", err)
	}
	if decoded.Request.Direction == nil || *decoded.Request.Direction != "callers" {
		t.Fatalf("direction = %v", decoded.Request.Direction)
	}
}

func TestDecodeEnvelopeAcceptsSchemaTwoNullDirectionForOtherOperations(t *testing.T) {
	envelope := validEnvelope()
	envelope.Request.SchemaVersion = "2"
	raw := envelopeWithRequestKeys(t, envelope, map[string]json.RawMessage{"direction": json.RawMessage("null")})
	decoded, err := DecodeEnvelope(bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("rejected schema-2 search-symbols with a null direction: %v", err)
	}
	if decoded.Request.Direction != nil {
		t.Fatalf("direction = %q", *decoded.Request.Direction)
	}
}

func TestDecodeEnvelopeRejectsInvalidSchemaTwoDirections(t *testing.T) {
	related := validEnvelope()
	related.Request = relatedRequest()
	for name, override := range map[string]map[string]json.RawMessage{
		"related without direction": {"direction": json.RawMessage("null")},
		"related missing key":       {"direction": nil},
		"unknown direction":         {"direction": json.RawMessage(`"sideways"`)},
	} {
		t.Run(name, func(t *testing.T) {
			raw := envelopeWithRequestKeys(t, related, override)
			if _, err := DecodeEnvelope(bytes.NewReader(raw)); !errors.Is(err, ErrInvalidWire) {
				t.Fatalf("error = %v", err)
			}
		})
	}
	directed := validEnvelope()
	directed.Request.SchemaVersion = "2"
	directed.Request.Direction = ptr("callers")
	raw, err := json.Marshal(directed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DecodeEnvelope(bytes.NewReader(append(raw, '\n'))); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("accepted a direction on search-symbols: %v", err)
	}
}

func TestRequestRejectsUnknownSchemaVersions(t *testing.T) {
	for _, version := range []string{"", "0", "5", "2.0"} {
		request := validRequest()
		request.SchemaVersion = version
		if err := ValidateRequest(request); err == nil {
			t.Fatalf("accepted schema version %q", version)
		}
	}
}

func TestRequestBoundsRelatedSymbolsAnchors(t *testing.T) {
	empty := relatedRequest()
	empty.ResultIdentities = []string{}
	if err := ValidateRequest(empty); err == nil {
		t.Fatal("accepted related-symbols without anchors")
	}
	withQuery := relatedRequest()
	withQuery.Query = ptr("anchor")
	if err := ValidateRequest(withQuery); err == nil {
		t.Fatal("accepted related-symbols carrying a query")
	}
	// The bound is part of the contract, so the two cases spell 16 and 17 out
	// instead of deriving them from the constant they are meant to pin.
	tooMany := relatedRequest()
	tooMany.ResultIdentities = make([]string, 0, 17)
	for index := 0; index < 17; index++ {
		tooMany.ResultIdentities = append(tooMany.ResultIdentities, fmt.Sprintf("sha256:%064x", index))
	}
	if err := ValidateRequest(tooMany); err == nil {
		t.Fatalf("accepted %d related-symbols anchors", len(tooMany.ResultIdentities))
	}
	bounded := relatedRequest()
	bounded.ResultIdentities = tooMany.ResultIdentities[:16]
	if err := ValidateRequest(bounded); err != nil {
		t.Fatalf("rejected 16 related-symbols anchors: %v", err)
	}
}

// TestValidateResultBindsEdgeFieldsToRelatedSymbols keeps the edge data where
// it was resolved: a schema-2 result of any other operation carries no
// relation, no edge evidence, and no reference numbers.
func TestValidateResultBindsEdgeFieldsToRelatedSymbols(t *testing.T) {
	// One field per subtest, in a fixed order, and each subtest sets that one
	// field on both halves: the same value is refused on another operation and
	// accepted on related-symbols.
	cases := []struct {
		name   string
		mutate func(*Result)
	}{
		{"relation", func(result *Result) { result.Findings[0].Relation = "call" }},
		{"evidence", func(result *Result) { result.Findings[0].EdgeEvidence = "verified" }},
		{"line", func(result *Result) { result.Findings[0].ReferenceLine = 7 }},
		{"count", func(result *Result) { result.Findings[0].ReferenceCount = 2 }},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			result := validResult()
			result.SchemaVersion = "2"
			testCase.mutate(&result)
			result.OutputCharacters = renderedOutputCharacters(result)
			if err := EncodeResult(ioDiscard{}, result); err == nil {
				t.Fatalf("accepted %s on a %s result", testCase.name, result.Operation)
			}
			related := relatedResult()
			related.Findings[0].Relation, related.Findings[0].EdgeEvidence = "", ""
			related.Findings[0].ReferenceLine, related.Findings[0].ReferenceCount = 0, 0
			testCase.mutate(&related)
			related.OutputCharacters = renderedOutputCharacters(related)
			if err := EncodeResult(ioDiscard{}, related); err != nil {
				t.Fatalf("rejected %s on a related-symbols result: %v", testCase.name, err)
			}
		})
	}
}

func relatedResult() Result {
	result := validResult()
	result.SchemaVersion = "2"
	result.Operation = RelatedSymbols
	result.Findings[0].Relation = "call"
	result.Findings[0].EdgeEvidence = "verified"
	result.Findings[0].ReferenceLine = 42
	result.Findings[0].ReferenceCount = 3
	result.OutputCharacters = renderedOutputCharacters(result)
	return result
}

func TestEncodeResultOmitsEdgeFieldsUnderSchemaOne(t *testing.T) {
	var schemaOne bytes.Buffer
	if err := EncodeResult(&schemaOne, validResult()); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"relation", "edge_evidence", "reference_line", "reference_count"} {
		if strings.Contains(schemaOne.String(), key) {
			t.Fatalf("schema-1 result carries %q: %s", key, schemaOne.String())
		}
	}
	var schemaTwo bytes.Buffer
	if err := EncodeResult(&schemaTwo, relatedResult()); err != nil {
		t.Fatal(err)
	}
	for _, fragment := range []string{`"relation":"call"`, `"edge_evidence":"verified"`, `"reference_line":42`, `"reference_count":3`} {
		if !strings.Contains(schemaTwo.String(), fragment) {
			t.Fatalf("schema-2 result missing %s: %s", fragment, schemaTwo.String())
		}
	}
}

func TestEncodeResultKeepsSchemaOneFindingKeysComplete(t *testing.T) {
	var schemaOne, schemaTwo bytes.Buffer
	if err := EncodeResult(&schemaOne, validResult()); err != nil {
		t.Fatal(err)
	}
	two := validResult()
	two.SchemaVersion = "2"
	if err := EncodeResult(&schemaTwo, two); err != nil {
		t.Fatal(err)
	}
	got, want := findingKeys(t, schemaOne.Bytes()), findingKeys(t, schemaTwo.Bytes())
	for _, key := range []string{"relation", "edge_evidence", "reference_line", "reference_count"} {
		delete(want, key)
	}
	if len(got) != len(want) {
		t.Fatalf("schema-1 finding keys = %v, want %v", got, want)
	}
	for key := range want {
		if _, ok := got[key]; !ok {
			t.Fatalf("schema-1 finding dropped %q", key)
		}
	}
}

func findingKeys(t *testing.T, encoded []byte) map[string]struct{} {
	t.Helper()
	var result struct {
		Findings []map[string]json.RawMessage `json:"findings"`
	}
	if err := json.Unmarshal(encoded, &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Findings) != 1 {
		t.Fatalf("findings = %d", len(result.Findings))
	}
	keys := map[string]struct{}{}
	for key := range result.Findings[0] {
		keys[key] = struct{}{}
	}
	return keys
}

func TestValidateResultRejectsInconsistentEdgeFields(t *testing.T) {
	cases := map[string]func(*Result){
		"schema one relation":      func(result *Result) { result.Findings[0].Relation = "call" },
		"schema one evidence":      func(result *Result) { result.Findings[0].EdgeEvidence = "verified" },
		"schema one line":          func(result *Result) { result.Findings[0].ReferenceLine = 1 },
		"schema one count":         func(result *Result) { result.Findings[0].ReferenceCount = 1 },
		"unknown relation":         func(result *Result) { result.SchemaVersion, result.Findings[0].Relation = "2", "sideways" },
		"unknown edge evidence":    func(result *Result) { result.SchemaVersion, result.Findings[0].EdgeEvidence = "2", "guessed" },
		"negative reference line":  func(result *Result) { result.SchemaVersion, result.Findings[0].ReferenceLine = "2", -1 },
		"negative reference count": func(result *Result) { result.SchemaVersion, result.Findings[0].ReferenceCount = "2", -1 },
		"unknown result schema":    func(result *Result) { result.SchemaVersion = "5" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			result := validResult()
			mutate(&result)
			result.OutputCharacters = renderedOutputCharacters(result)
			if err := EncodeResult(ioDiscard{}, result); err == nil {
				t.Fatalf("accepted %s", name)
			}
		})
	}
}

func TestOutputCharactersCoverSchemaTwoEdgeFields(t *testing.T) {
	related := relatedResult()
	plain := relatedResult()
	plain.Findings[0].Relation, plain.Findings[0].EdgeEvidence = "", ""
	plain.Findings[0].ReferenceLine, plain.Findings[0].ReferenceCount = 0, 0
	got := renderedOutputCharacters(related) - renderedOutputCharacters(plain)
	if want := len(" relation=call edge=verified ref=42x3"); got != want {
		t.Fatalf("edge annotation characters = %d, want %d", got, want)
	}
	frozen := validResult()
	frozen.SchemaVersion = "2"
	if OutputCharacters(frozen) != renderedOutputCharacters(validResult()) {
		t.Fatal("schema-2 findings without a relation changed the frozen calculation")
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

// --- schema 3: changed-symbols and changed ranges ---------------------------

// changedRanges builds the request-side selector; the pointed-to slice is never
// nil so an empty selector still marshals as [] rather than null.
func changedRanges(entries ...ChangedRange) *[]ChangedRange {
	list := make([]ChangedRange, 0, len(entries))
	list = append(list, entries...)
	return &list
}

func changedRequest() Request {
	request := validRequest()
	request.SchemaVersion = "3"
	request.Operation, request.RequiredCapability = ChangedSymbols, "changed-symbols"
	request.Query = nil
	request.ResultIdentities = []string{}
	request.ChangedRanges = changedRanges(
		ChangedRange{Path: "internal/query/changed.go", Ranges: [][2]int{{10, 20}, {40, 40}}},
		ChangedRange{Path: "tools/taf-context/taf_context/recovery.py", Ranges: [][2]int{}},
	)
	return request
}

// framedEnvelope marshals an envelope for the decoder and spells out the
// schema-3 keys that struct marshaling omits: the null direction and, for the
// operations that carry no selector, the null changed_ranges. Schema 4 carries
// the same key set with both selectors null.
func framedEnvelope(t *testing.T, envelope Envelope) []byte {
	t.Helper()
	overrides := map[string]json.RawMessage{}
	if envelope.Request.SchemaVersion == "3" || envelope.Request.SchemaVersion == "4" {
		overrides["direction"] = json.RawMessage("null")
		if envelope.Request.ChangedRanges == nil {
			overrides["changed_ranges"] = json.RawMessage("null")
		}
	}
	return envelopeWithRequestKeys(t, envelope, overrides)
}

// changedEnvelopeWith replaces the changed_ranges value of a schema-3
// changed-symbols envelope; an empty raw value removes the key entirely.
func changedEnvelopeWith(t *testing.T, raw string) []byte {
	t.Helper()
	envelope := validEnvelope()
	envelope.Request = changedRequest()
	overrides := map[string]json.RawMessage{"direction": json.RawMessage("null")}
	if raw == "" {
		overrides["changed_ranges"] = nil
	} else {
		overrides["changed_ranges"] = json.RawMessage(raw)
	}
	return envelopeWithRequestKeys(t, envelope, overrides)
}

func TestDecodeEnvelopeRejectsChangedRangesUnderFrozenSchemas(t *testing.T) {
	for _, schemaVersion := range []string{"1", "2"} {
		envelope := validEnvelope()
		envelope.Request.SchemaVersion = schemaVersion
		overrides := map[string]json.RawMessage{"changed_ranges": json.RawMessage(`[{"path":"a.go","ranges":[[1,2]]}]`)}
		if schemaVersion == "2" {
			overrides["direction"] = json.RawMessage("null")
		}
		raw := envelopeWithRequestKeys(t, envelope, overrides)
		if _, err := DecodeEnvelope(bytes.NewReader(raw)); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-%s changed_ranges: error = %v", schemaVersion, err)
		}
		request := validRequest()
		request.SchemaVersion = schemaVersion
		request.ChangedRanges = changedRanges(ChangedRange{Path: "a.go", Ranges: [][2]int{{1, 2}}})
		if err := ValidateRequest(request); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("typed schema-%s changed ranges: error = %v", schemaVersion, err)
		}
	}
}

func TestDecodeEnvelopeAcceptsSchemaThreeChangedSymbols(t *testing.T) {
	envelope := validEnvelope()
	envelope.Request = changedRequest()
	decoded, err := DecodeEnvelope(bytes.NewReader(framedEnvelope(t, envelope)))
	if err != nil {
		t.Fatalf("rejected schema-3 changed-symbols: %v", err)
	}
	if decoded.Request.ChangedRanges == nil {
		t.Fatal("changed ranges dropped")
	}
	entries := *decoded.Request.ChangedRanges
	if len(entries) != 2 || entries[0].Path != "internal/query/changed.go" || len(entries[0].Ranges) != 2 || entries[0].Ranges[1] != [2]int{40, 40} || len(entries[1].Ranges) != 0 {
		t.Fatalf("changed ranges = %+v", entries)
	}
	if decoded.Request.Direction != nil {
		t.Fatalf("direction = %q", *decoded.Request.Direction)
	}
}

func TestDecodeEnvelopeAcceptsSchemaThreeNullChangedRangesForOtherOperations(t *testing.T) {
	envelope := validEnvelope()
	envelope.Request.SchemaVersion = "3"
	decoded, err := DecodeEnvelope(bytes.NewReader(framedEnvelope(t, envelope)))
	if err != nil {
		t.Fatalf("rejected schema-3 search-symbols with null changed ranges: %v", err)
	}
	if decoded.Request.ChangedRanges != nil {
		t.Fatalf("changed ranges = %+v", *decoded.Request.ChangedRanges)
	}
}

func TestDecodeEnvelopeRejectsMalformedChangedRanges(t *testing.T) {
	manyPaths := make([]string, 0, 201)
	for index := 0; index < 201; index++ {
		manyPaths = append(manyPaths, fmt.Sprintf(`{"path":"a/p%03d.go","ranges":[[1,2]]}`, index))
	}
	manyRanges := make([]string, 0, 65)
	for index := 0; index < 65; index++ {
		manyRanges = append(manyRanges, fmt.Sprintf("[%d,%d]", 2*index+1, 2*index+1))
	}
	cases := map[string]string{
		"null selector":       "null",
		"missing key":         "",
		"not an array":        `{"path":"a.go","ranges":[[1,2]]}`,
		"missing path key":    `[{"ranges":[[1,2]]}]`,
		"missing ranges key":  `[{"path":"a.go"}]`,
		"unknown entry key":   `[{"path":"a.go","ranges":[[1,2]],"note":"x"}]`,
		"null path":           `[{"path":null,"ranges":[[1,2]]}]`,
		"null ranges":         `[{"path":"a.go","ranges":null}]`,
		"absolute path":       `[{"path":"/a.go","ranges":[[1,2]]}]`,
		"parent path":         `[{"path":"../a.go","ranges":[[1,2]]}]`,
		"unsorted paths":      `[{"path":"b.go","ranges":[]},{"path":"a.go","ranges":[]}]`,
		"duplicate paths":     `[{"path":"a.go","ranges":[]},{"path":"a.go","ranges":[]}]`,
		"descending bounds":   `[{"path":"a.go","ranges":[[5,3]]}]`,
		"zero start":          `[{"path":"a.go","ranges":[[0,1]]}]`,
		"overflowing end":     `[{"path":"a.go","ranges":[[1,2147483648]]}]`,
		"three element range": `[{"path":"a.go","ranges":[[1,2,3]]}]`,
		"one element range":   `[{"path":"a.go","ranges":[[1]]}]`,
		"string bounds":       `[{"path":"a.go","ranges":[["1","2"]]}]`,
		"unsorted ranges":     `[{"path":"a.go","ranges":[[5,6],[1,2]]}]`,
		"overlapping ranges":  `[{"path":"a.go","ranges":[[1,4],[4,6]]}]`,
		"too many paths":      "[" + strings.Join(manyPaths, ",") + "]",
		"too many ranges":     `[{"path":"a.go","ranges":[` + strings.Join(manyRanges, ",") + `]}]`,
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeEnvelope(bytes.NewReader(changedEnvelopeWith(t, raw))); !errors.Is(err, ErrInvalidWire) {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestRequestBoundsChangedRanges(t *testing.T) {
	// The bounds are part of the contract, so the cases spell 200 and 64 out
	// instead of deriving them from the constants they are meant to pin.
	bounded := changedRequest()
	entries := make([]ChangedRange, 0, 200)
	for index := 0; index < 200; index++ {
		entries = append(entries, ChangedRange{Path: fmt.Sprintf("a/p%03d.go", index), Ranges: [][2]int{{1, 2}}})
	}
	bounded.ChangedRanges = changedRanges(entries...)
	if err := ValidateRequest(bounded); err != nil {
		t.Fatalf("rejected 200 changed paths: %v", err)
	}
	tooMany := changedRequest()
	tooMany.ChangedRanges = changedRanges(append(entries, ChangedRange{Path: "a/p200.go", Ranges: [][2]int{{1, 2}}})...)
	if err := ValidateRequest(tooMany); err == nil {
		t.Fatal("accepted 201 changed paths")
	}
	spans := make([][2]int, 0, 64)
	for index := 0; index < 64; index++ {
		spans = append(spans, [2]int{2*index + 1, 2*index + 1})
	}
	boundedSpans := changedRequest()
	boundedSpans.ChangedRanges = changedRanges(ChangedRange{Path: "a.go", Ranges: spans})
	if err := ValidateRequest(boundedSpans); err != nil {
		t.Fatalf("rejected 64 ranges: %v", err)
	}
	tooManySpans := changedRequest()
	tooManySpans.ChangedRanges = changedRanges(ChangedRange{Path: "a.go", Ranges: append(spans, [2]int{129, 129})})
	if err := ValidateRequest(tooManySpans); err == nil {
		t.Fatal("accepted 65 ranges")
	}
	empty := changedRequest()
	empty.ChangedRanges = changedRanges()
	if err := ValidateRequest(empty); err != nil {
		t.Fatalf("rejected an empty changed selector: %v", err)
	}
}

func TestRequestBindsChangedSymbolsAndRelatedSymbolsToTheirSchemas(t *testing.T) {
	for _, schemaVersion := range []string{"1", "2"} {
		request := changedRequest()
		request.SchemaVersion = schemaVersion
		request.ChangedRanges = nil
		if err := ValidateRequest(request); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-%s changed-symbols: error = %v", schemaVersion, err)
		}
	}
	related := relatedRequest()
	related.SchemaVersion = "3"
	if err := ValidateRequest(related); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("schema-3 related-symbols: error = %v", err)
	}
	missing := changedRequest()
	missing.ChangedRanges = nil
	if err := ValidateRequest(missing); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("changed-symbols without a selector: error = %v", err)
	}
	uninvited := validRequest()
	uninvited.SchemaVersion = "3"
	uninvited.ChangedRanges = changedRanges(ChangedRange{Path: "a.go", Ranges: [][2]int{{1, 2}}})
	if err := ValidateRequest(uninvited); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("search-symbols carrying a selector: error = %v", err)
	}
	withQuery := changedRequest()
	withQuery.Query = ptr("anchor")
	if err := ValidateRequest(withQuery); err == nil {
		t.Fatal("accepted changed-symbols carrying a query")
	}
	withAnchors := changedRequest()
	withAnchors.ResultIdentities = []string{resultIdentity}
	if err := ValidateRequest(withAnchors); err == nil {
		t.Fatal("accepted changed-symbols carrying anchors")
	}
	withDirection := changedRequest()
	withDirection.Direction = ptr("callers")
	if err := ValidateRequest(withDirection); err == nil {
		t.Fatal("accepted changed-symbols carrying a direction")
	}
}

func changedResult() Result {
	result := validResult()
	result.SchemaVersion = "3"
	result.Operation = ChangedSymbols
	result.OutputCharacters = renderedOutputCharacters(result)
	return result
}

func TestEncodeResultAcceptsSchemaThreeChangedSymbols(t *testing.T) {
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, changedResult()); err != nil {
		t.Fatalf("rejected a schema-3 changed-symbols result: %v", err)
	}
	got, want := findingKeys(t, encoded.Bytes()), map[string]struct{}{}
	var schemaTwo bytes.Buffer
	two := validResult()
	two.SchemaVersion = "2"
	if err := EncodeResult(&schemaTwo, two); err != nil {
		t.Fatal(err)
	}
	want = findingKeys(t, schemaTwo.Bytes())
	if len(got) != len(want) {
		t.Fatalf("schema-3 finding keys = %v, want %v", got, want)
	}
	for key := range want {
		if _, ok := got[key]; !ok {
			t.Fatalf("schema-3 finding dropped %q", key)
		}
	}
}

func TestValidateResultBindsSchemaThreeToChangedSymbols(t *testing.T) {
	related := validResult()
	related.SchemaVersion = "3"
	related.Operation = RelatedSymbols
	related.OutputCharacters = renderedOutputCharacters(related)
	if err := EncodeResult(ioDiscard{}, related); err == nil {
		t.Fatal("accepted a schema-3 related-symbols result")
	}
	for _, schemaVersion := range []string{"1", "2"} {
		result := validResult()
		result.SchemaVersion = schemaVersion
		result.Operation = ChangedSymbols
		result.OutputCharacters = renderedOutputCharacters(result)
		if err := EncodeResult(ioDiscard{}, result); err == nil {
			t.Fatalf("accepted a schema-%s changed-symbols result", schemaVersion)
		}
	}
	edged := changedResult()
	edged.Findings[0].Relation = "call"
	edged.OutputCharacters = renderedOutputCharacters(edged)
	if err := EncodeResult(ioDiscard{}, edged); err == nil {
		t.Fatal("accepted edge fields on a schema-3 result")
	}
}

func TestMarshalRequestKeepsChangedRangesOutOfFrozenSchemas(t *testing.T) {
	for _, request := range []Request{validRequest(), relatedRequest()} {
		raw, err := json.Marshal(request)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(raw), "changed_ranges") {
			t.Fatalf("schema-%s request carries changed_ranges: %s", request.SchemaVersion, raw)
		}
	}
	raw, err := json.Marshal(changedRequest())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"changed_ranges":[{"path":"internal/query/changed.go","ranges":[[10,20],[40,40]]}`) {
		t.Fatalf("schema-3 request selector = %s", raw)
	}
}

// --- schema 4: repository-overview groups -----------------------------------

// overviewFilters is the filter set the overview accepts: it counts files, so
// it takes path prefixes and languages and refuses the two symbol-shaped lists.
func overviewFilters() Filters {
	return Filters{PathPrefixes: []string{"tools/taf-context"}, Languages: []string{"Python"}, SymbolKinds: []string{}, SourceTypes: []string{}}
}

func overviewRequest() Request {
	request := validRequest()
	request.SchemaVersion = "4"
	request.Operation, request.RequiredCapability = RepositoryOverview, "repository-overview"
	request.Query = nil
	request.ResultIdentities = []string{}
	request.Filters = overviewFilters()
	return request
}

// overviewGroups builds the result-side group list; the pointed-to slice is
// never nil so an empty overview still marshals as [] rather than null.
func overviewGroups(entries ...OverviewGroup) *[]OverviewGroup {
	list := make([]OverviewGroup, 0, len(entries))
	list = append(list, entries...)
	return &list
}

// overviewResult carries a full schema-4 payload: two directory groups and a
// "*" row. The engine never emits that row — a consumer folding a table to an
// output budget produces it — but the wire has to keep admitting it.
func overviewResult() Result {
	result := validResult()
	result.SchemaVersion = "4"
	result.Operation = RepositoryOverview
	result.Groups = overviewGroups(
		OverviewGroup{
			PathPrefix: "internal/", Depth: 1, FileCount: 12, DefinitionCount: 40, EntryPointCount: 0,
			DocumentCount: 0, ConfigurationCount: 0,
			Languages:              []OverviewLanguage{{Language: "Go", FileCount: 12}},
			RepresentativeIdentity: ptr(resultIdentity),
		},
		OverviewGroup{
			PathPrefix: "cmd/taf-level1/.", Depth: 2, FileCount: 2, DefinitionCount: 3, EntryPointCount: 1,
			DocumentCount: 0, ConfigurationCount: 0,
			Languages:              []OverviewLanguage{{Language: "Go", FileCount: 2}},
			RepresentativeIdentity: ptr(overviewIdentity),
		},
		OverviewGroup{
			PathPrefix: "*", Depth: 0, FileCount: 5, DefinitionCount: 0, EntryPointCount: 0,
			DocumentCount: 3, ConfigurationCount: 2,
			Languages:              []OverviewLanguage{{Language: "Markdown", FileCount: 3}, {Language: "JSON", FileCount: 2}},
			RepresentativeIdentity: nil,
		},
	)
	result.Overview = &OverviewSummary{Root: "", CountedFileCount: 19, OtherGroupCount: 4}
	result.OutputCharacters = renderedOutputCharacters(result)
	return result
}

func TestDecodeEnvelopeAcceptsSchemaFourRepositoryOverview(t *testing.T) {
	envelope := validEnvelope()
	envelope.Request = overviewRequest()
	decoded, err := DecodeEnvelope(bytes.NewReader(framedEnvelope(t, envelope)))
	if err != nil {
		t.Fatalf("rejected schema-4 repository-overview: %v", err)
	}
	if decoded.Request.Operation != RepositoryOverview || decoded.Request.SchemaVersion != "4" {
		t.Fatalf("request = %s/%s", decoded.Request.SchemaVersion, decoded.Request.Operation)
	}
	if decoded.Request.Direction != nil || decoded.Request.ChangedRanges != nil {
		t.Fatalf("schema-4 request carries a selector: %v/%v", decoded.Request.Direction, decoded.Request.ChangedRanges)
	}
}

// The schema-4 request key set is the schema-3 one, so a request that omits
// either null selector is malformed even though the typed value would be nil.
func TestDecodeEnvelopeRequiresBothNullSelectorsUnderSchemaFour(t *testing.T) {
	for _, field := range []string{"direction", "changed_ranges"} {
		envelope := validEnvelope()
		envelope.Request = overviewRequest()
		overrides := map[string]json.RawMessage{"direction": json.RawMessage("null"), "changed_ranges": json.RawMessage("null")}
		overrides[field] = nil
		raw := envelopeWithRequestKeys(t, envelope, overrides)
		if _, err := DecodeEnvelope(bytes.NewReader(raw)); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-4 request without %s: error = %v", field, err)
		}
	}
}

func TestRequestBindsRepositoryOverviewToSchemaFour(t *testing.T) {
	for _, schemaVersion := range []string{"1", "2", "3"} {
		request := overviewRequest()
		request.SchemaVersion = schemaVersion
		if err := ValidateRequest(request); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-%s repository-overview: error = %v", schemaVersion, err)
		}
	}
	related := relatedRequest()
	related.SchemaVersion = "4"
	related.Direction = nil
	if err := ValidateRequest(related); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("schema-4 related-symbols: error = %v", err)
	}
	changed := changedRequest()
	changed.SchemaVersion = "4"
	changed.ChangedRanges = nil
	if err := ValidateRequest(changed); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("schema-4 changed-symbols: error = %v", err)
	}
	// A schema-agnostic operation may travel under schema 4.
	search := validRequest()
	search.SchemaVersion = "4"
	if err := ValidateRequest(search); err != nil {
		t.Fatalf("rejected schema-4 search-symbols: %v", err)
	}
}

// The overview counts files, so the two symbol-shaped filters would promise a
// narrowing it cannot perform; path prefixes and languages are accepted.
func TestRequestRejectsSymbolShapedFiltersForRepositoryOverview(t *testing.T) {
	kinds := overviewRequest()
	kinds.Filters.SymbolKinds = []string{"class"}
	if err := ValidateRequest(kinds); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("symbol kinds: error = %v", err)
	}
	sources := overviewRequest()
	sources.Filters.SourceTypes = []string{"source"}
	if err := ValidateRequest(sources); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("source types: error = %v", err)
	}
	if err := ValidateRequest(overviewRequest()); err != nil {
		t.Fatalf("rejected path prefix and language filters: %v", err)
	}
}

func TestRequestRejectsRepositoryOverviewExtras(t *testing.T) {
	cases := map[string]func(*Request){
		"query":     func(request *Request) { request.Query = ptr("anchor") },
		"anchors":   func(request *Request) { request.ResultIdentities = []string{resultIdentity} },
		"direction": func(request *Request) { request.Direction = ptr("callers") },
		"changed ranges": func(request *Request) {
			request.ChangedRanges = changedRanges(ChangedRange{Path: "a.go", Ranges: [][2]int{{1, 2}}})
		},
		"no index": func(request *Request) { request.IndexIdentity = nil },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			request := overviewRequest()
			mutate(&request)
			if err := ValidateRequest(request); err == nil {
				t.Fatalf("accepted repository-overview carrying %s", name)
			}
		})
	}
}

func TestEncodeResultAcceptsSchemaFourRepositoryOverview(t *testing.T) {
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, overviewResult()); err != nil {
		t.Fatalf("rejected a schema-4 repository-overview result: %v", err)
	}
	for _, fragment := range []string{`"groups":[`, `"path_prefix":"internal/"`, `"overview":{`, `"counted_file_count":19`, `"other_group_count":4`, `"representative_identity":null`} {
		if !strings.Contains(encoded.String(), fragment) {
			t.Fatalf("schema-4 result missing %s: %s", fragment, encoded.String())
		}
	}
	// Schema-4 findings carry the schema-2 edge keys, zeroed.
	var schemaTwo bytes.Buffer
	two := validResult()
	two.SchemaVersion = "2"
	if err := EncodeResult(&schemaTwo, two); err != nil {
		t.Fatal(err)
	}
	got, want := findingKeys(t, encoded.Bytes()), findingKeys(t, schemaTwo.Bytes())
	if len(got) != len(want) {
		t.Fatalf("schema-4 finding keys = %v, want %v", got, want)
	}
	for key := range want {
		if _, ok := got[key]; !ok {
			t.Fatalf("schema-4 finding dropped %q", key)
		}
	}
}

// An empty group list is a legitimate schema-4 payload, and a schema-agnostic
// operation may carry one: the two keys belong to the schema, not to the
// operation that introduced them.
func TestValidateResultAcceptsEmptyGroupsUnderSchemaFour(t *testing.T) {
	result := validResult()
	result.SchemaVersion = "4"
	result.Groups = overviewGroups()
	result.Overview = &OverviewSummary{Root: "tools/", CountedFileCount: 0, OtherGroupCount: 0}
	result.OutputCharacters = renderedOutputCharacters(result)
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, result); err != nil {
		t.Fatalf("rejected an empty schema-4 group list: %v", err)
	}
	if !strings.Contains(encoded.String(), `"groups":[]`) {
		t.Fatalf("empty group list did not marshal as []: %s", encoded.String())
	}
}

func TestValidateResultRejectsOverviewKeysOutsideSchemaFour(t *testing.T) {
	for _, schemaVersion := range []string{"1", "2", "3"} {
		for name, mutate := range map[string]func(*Result){
			"groups":   func(result *Result) { result.Groups = overviewGroups() },
			"overview": func(result *Result) { result.Overview = &OverviewSummary{} },
		} {
			result := validResult()
			result.SchemaVersion = schemaVersion
			if schemaVersion == "3" {
				result.Operation = ChangedSymbols
			}
			mutate(&result)
			result.OutputCharacters = renderedOutputCharacters(result)
			if err := EncodeResult(ioDiscard{}, result); !errors.Is(err, ErrInvalidWire) {
				t.Fatalf("schema-%s result carrying %s: error = %v", schemaVersion, name, err)
			}
		}
	}
}

// Struct marshaling omits a nil group list and a nil summary, so a schema-4
// result that left either unset would travel without a key the schema promises.
func TestValidateResultRequiresBothOverviewKeysUnderSchemaFour(t *testing.T) {
	for name, mutate := range map[string]func(*Result){
		"missing groups":   func(result *Result) { result.Groups = nil },
		"missing overview": func(result *Result) { result.Overview = nil },
		"missing both":     func(result *Result) { result.Groups, result.Overview = nil, nil },
	} {
		t.Run(name, func(t *testing.T) {
			result := overviewResult()
			mutate(&result)
			result.OutputCharacters = renderedOutputCharacters(result)
			if err := EncodeResult(ioDiscard{}, result); !errors.Is(err, ErrInvalidWire) {
				t.Fatalf("error = %v", err)
			}
		})
	}
	// repository-overview itself exists only under schema 4.
	for _, schemaVersion := range []string{"1", "2", "3"} {
		result := validResult()
		result.SchemaVersion = schemaVersion
		result.Operation = RepositoryOverview
		result.OutputCharacters = renderedOutputCharacters(result)
		if err := EncodeResult(ioDiscard{}, result); !errors.Is(err, ErrInvalidWire) {
			t.Fatalf("schema-%s repository-overview result: error = %v", schemaVersion, err)
		}
	}
}

func TestValidateResultRejectsMalformedOverviewRows(t *testing.T) {
	cases := map[string]func(*Result){
		"empty prefix":          func(result *Result) { (*result.Groups)[0].PathPrefix = "" },
		"absolute prefix":       func(result *Result) { (*result.Groups)[0].PathPrefix = "/internal/" },
		"parent prefix":         func(result *Result) { (*result.Groups)[0].PathPrefix = "../internal/" },
		"empty segment":         func(result *Result) { (*result.Groups)[0].PathPrefix = "internal//" },
		"bare directory":        func(result *Result) { (*result.Groups)[0].PathPrefix = "internal" },
		"negative depth":        func(result *Result) { (*result.Groups)[0].Depth = -1 },
		"negative files":        func(result *Result) { (*result.Groups)[0].FileCount = -1 },
		"negative definitions":  func(result *Result) { (*result.Groups)[0].DefinitionCount = -1 },
		"negative entry points": func(result *Result) { (*result.Groups)[0].EntryPointCount = -1 },
		"negative documents":    func(result *Result) { (*result.Groups)[0].DocumentCount = -1 },
		"negative configs":      func(result *Result) { (*result.Groups)[0].ConfigurationCount = -1 },
		"nil languages":         func(result *Result) { (*result.Groups)[0].Languages = nil },
		"empty language name":   func(result *Result) { (*result.Groups)[0].Languages[0].Language = "" },
		"negative language":     func(result *Result) { (*result.Groups)[0].Languages[0].FileCount = -1 },
		"ascending languages": func(result *Result) {
			(*result.Groups)[2].Languages = []OverviewLanguage{{Language: "JSON", FileCount: 2}, {Language: "Markdown", FileCount: 3}}
		},
		"duplicate languages": func(result *Result) {
			(*result.Groups)[2].Languages = []OverviewLanguage{{Language: "JSON", FileCount: 2}, {Language: "JSON", FileCount: 2}}
		},
		"unsorted equal counts": func(result *Result) {
			(*result.Groups)[2].Languages = []OverviewLanguage{{Language: "Markdown", FileCount: 2}, {Language: "JSON", FileCount: 2}}
		},
		"bad representative":   func(result *Result) { (*result.Groups)[0].RepresentativeIdentity = ptr("not-a-digest") },
		"fold representative":  func(result *Result) { (*result.Groups)[2].RepresentativeIdentity = ptr(resultIdentity) },
		"bad summary root":     func(result *Result) { result.Overview.Root = "/tools/" },
		"bare summary root":    func(result *Result) { result.Overview.Root = "tools" },
		"fold summary root":    func(result *Result) { result.Overview.Root = "*" },
		"negative counted":     func(result *Result) { result.Overview.CountedFileCount = -1 },
		"negative other count": func(result *Result) { result.Overview.OtherGroupCount = -1 },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			result := overviewResult()
			mutate(&result)
			result.OutputCharacters = renderedOutputCharacters(result)
			if err := EncodeResult(ioDiscard{}, result); !errors.Is(err, ErrInvalidWire) {
				t.Fatalf("accepted %s: error = %v", name, err)
			}
		})
	}
}

// overviewRows builds count plausible directory rows, so a test can say how
// wide a table it means rather than how it is spelled.
func overviewRows(count int) []OverviewGroup {
	rows := make([]OverviewGroup, 0, count)
	for index := 0; index < count; index++ {
		rows = append(rows, OverviewGroup{
			PathPrefix: fmt.Sprintf("directory%04d/", index), Depth: 1, FileCount: 1,
			Languages: []OverviewLanguage{{Language: "Go", FileCount: 1}}, RepresentativeIdentity: ptr(resultIdentity),
		})
	}
	return rows
}

// A hundred rows is an ordinary wide table and travels the whole encode path.
func TestEncodeResultAcceptsAWideOverviewTable(t *testing.T) {
	wide := overviewResult()
	wide.Groups = overviewGroups(overviewRows(100)...)
	wide.OutputCharacters = renderedOutputCharacters(wide)
	if err := EncodeResult(ioDiscard{}, wide); err != nil {
		t.Fatalf("rejected 100 group rows: %v", err)
	}
}

// The bound is part of the contract, so the cases spell 4096 and 4097 out
// instead of deriving them from the constant they are meant to pin: a table
// can hold at most one row per indexed path, and nothing beyond that is a
// directory table at all. Validation is called directly here because a result
// this wide exceeds the transport byte cap, which would otherwise reject the
// admitted case for a reason that has nothing to do with the row bound.
func TestValidateResultBoundsOverviewGroupRows(t *testing.T) {
	rows := overviewRows(4097)
	bounded := overviewResult()
	bounded.Groups = overviewGroups(rows[:4096]...)
	bounded.OutputCharacters = renderedOutputCharacters(bounded)
	if err := validateResult(bounded); err != nil {
		t.Fatalf("rejected 4096 group rows: %v", err)
	}
	tooMany := overviewResult()
	tooMany.Groups = overviewGroups(rows...)
	tooMany.OutputCharacters = renderedOutputCharacters(tooMany)
	if err := validateResult(tooMany); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("accepted 4097 group rows: error = %v", err)
	}
}

func TestValidateResultAcceptsEveryGroupPrefixShape(t *testing.T) {
	for _, prefix := range []string{".", "*", "internal/", "internal/query/", "cmd/taf-level1/."} {
		result := overviewResult()
		(*result.Groups)[0].PathPrefix = prefix
		if prefix == "*" {
			(*result.Groups)[0].RepresentativeIdentity = nil
		}
		result.OutputCharacters = renderedOutputCharacters(result)
		if err := EncodeResult(ioDiscard{}, result); err != nil {
			t.Fatalf("rejected group prefix %q: %v", prefix, err)
		}
	}
}

// The group row and the summary are exact key sets: a consumer that reads them
// positionally must never meet a renamed, added, or dropped key.
func TestEncodeResultKeepsOverviewKeySetsExact(t *testing.T) {
	var encoded bytes.Buffer
	if err := EncodeResult(&encoded, overviewResult()); err != nil {
		t.Fatal(err)
	}
	var decoded struct {
		Groups   []map[string]json.RawMessage `json:"groups"`
		Overview map[string]json.RawMessage   `json:"overview"`
	}
	if err := json.Unmarshal(encoded.Bytes(), &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Groups) != 3 {
		t.Fatalf("groups = %d", len(decoded.Groups))
	}
	wantGroup := []string{"path_prefix", "depth", "file_count", "definition_count", "entry_point_count", "document_count", "configuration_count", "languages", "representative_identity"}
	for index, group := range decoded.Groups {
		assertExactKeys(t, fmt.Sprintf("group %d", index), group, wantGroup)
		var languages []map[string]json.RawMessage
		if err := json.Unmarshal(group["languages"], &languages); err != nil {
			t.Fatal(err)
		}
		for _, language := range languages {
			assertExactKeys(t, "language", language, []string{"language", "file_count"})
		}
	}
	assertExactKeys(t, "overview", decoded.Overview, []string{"root", "counted_file_count", "other_group_count"})
}

func assertExactKeys(t *testing.T, name string, value map[string]json.RawMessage, want []string) {
	t.Helper()
	if len(value) != len(want) {
		t.Fatalf("%s keys = %v, want %v", name, value, want)
	}
	for _, key := range want {
		if _, ok := value[key]; !ok {
			t.Fatalf("%s is missing %q", name, key)
		}
	}
}

// The frozen schemas keep their exact result key set: the two schema-4 keys are
// omitted entirely rather than spelled out as null.
func TestMarshalResultKeepsOverviewKeysOutOfFrozenSchemas(t *testing.T) {
	for _, result := range []Result{validResult(), relatedResult(), changedResult()} {
		var encoded bytes.Buffer
		if err := EncodeResult(&encoded, result); err != nil {
			t.Fatal(err)
		}
		for _, key := range []string{"groups", "overview"} {
			if strings.Contains(encoded.String(), `"`+key+`"`) {
				t.Fatalf("schema-%s result carries %q: %s", result.SchemaVersion, key, encoded.String())
			}
		}
	}
}

// The row bound is not the bound a real table meets. A table far below 4096
// rows already exceeds the transport frame, and EncodeResult is where that
// shows: the row count the frame can carry is what governs a wide table, and
// TestValidateResultBoundsOverviewGroupRows calls validation directly for
// exactly that reason. Pinning it here keeps the operative ceiling visible
// rather than leaving the suite to speak only of a bound production can never
// reach.
func TestEncodeResultRejectsATableTheTransportCannotCarry(t *testing.T) {
	const rows = 2000
	if rows >= MaximumOverviewGroups {
		t.Fatalf("the fixture must stay below the row bound of %d rows", MaximumOverviewGroups)
	}
	wide := overviewResult()
	wide.Groups = overviewGroups(overviewRows(rows)...)
	wide.OutputCharacters = renderedOutputCharacters(wide)
	// The row bound admits the table; only the frame refuses it.
	if err := validateResult(wide); err != nil {
		t.Fatalf("the row bound rejected %d rows: %v", rows, err)
	}
	if err := EncodeResult(ioDiscard{}, wide); !errors.Is(err, ErrInvalidWire) {
		t.Fatalf("a table of %d rows encoded within %d bytes: error = %v", rows, policy.ProductionLimits().MaximumStdoutBytes, err)
	}
}
