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
		CommittedHead: head, DirtyOverlayFingerprint: dirtyIdentity, ProviderIdentity: "taf.native.level1",
		IndexIdentity: ptr(indexIdentity), RequiredCapability: "search-symbols", MinimumFreshness: "exact",
		Query: ptr("RecoveryDossier"), ResultIdentities: []string{},
		Filters:        Filters{PathPrefixes: []string{"tools/taf-context"}, Languages: []string{"Python"}, SymbolKinds: []string{"class"}, SourceTypes: []string{"source"}},
		MaximumResults: 10, MaximumModelOutputCharacters: 4000, AllowInferred: false,
	}
}

func validEnvelope() Envelope {
	return Envelope{Phase: "query", RepositoryRoot: "/repo", StateRoot: "/state", Request: validRequest()}
}

func ptr(value string) *string { return &value }

func TestDecodeEnvelopeRejectsDuplicateRequestKey(t *testing.T) {
	raw := `{"phase":"query","repository_root":"/repo","state_root":"/state","changed_paths_document":null,"request":{"schema_version":"1","schema_version":"1"}}` + "\n"
	_, err := DecodeEnvelope(strings.NewReader(raw))
	if !errors.Is(err, ErrDuplicateKey) {
		t.Fatalf("error = %v", err)
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

func TestDecodeEnvelopeEnforcesQueryRootsAndChangedPathDocument(t *testing.T) {
	raw, _ := json.Marshal(validEnvelope())
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatal(err)
	}
	for field, value := range map[string]json.RawMessage{"phase": json.RawMessage(`"inspect"`), "repository_root": json.RawMessage(`"relative"`), "state_root": json.RawMessage(`"relative"`), "changed_paths_document": json.RawMessage(`"bad\nvalue"`)} {
		copy := cloneRawMap(envelope)
		copy[field] = value
		encoded, _ := json.Marshal(copy)
		if _, err := DecodeEnvelope(bytes.NewReader(append(encoded, '\n'))); err == nil {
			t.Fatalf("accepted invalid %s", field)
		}
	}
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
	for _, request := range cases {
		if err := ValidateRequest(request); err == nil {
			t.Fatalf("accepted invalid request: %+v", request)
		}
	}
}

func validResult() Result {
	return Result{
		SchemaVersion: "1", RequestIdentity: "request-0001", Operation: SearchSymbols, Status: Ready,
		ProviderIdentity: "taf.native.level1", ProviderVersion: "0.1.0", IndexIdentity: ptr(indexIdentity),
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
