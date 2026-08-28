package wire

import (
	"bytes"
	"errors"
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

func TestRequestRequiresOperationCapabilityParity(t *testing.T) {
	request := validRequest()
	request.RequiredCapability = "search-docs"
	if err := ValidateRequest(request); !errors.Is(err, ErrRequiredCapability) {
		t.Fatalf("error = %v", err)
	}
}

func TestRequestAcceptsEveryFrozenOperation(t *testing.T) {
	for _, operation := range AllOperations {
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
		ReturnedCount: 1, OmittedCount: 0, Truncated: false, Warnings: []string{}, NextSafeAction: "use-cited-evidence",
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
}
