package model

import "testing"

func TestNormalizedTypesCarryAllDownstreamBindings(t *testing.T) {
	record := Record{Identity: "record", Path: "pkg/service.go", StartLine: 1, EndLine: 2, RecordKind: Definition, EvidenceClass: Verified, SearchTerms: []string{"service", "run"}, SourceDigest: "sha256:source"}
	manifest := Manifest{FormatVersion: "1", EngineVersion: "1", Binding: Binding{RepositoryIdentity: "sha256:repo", WorktreeIdentity: "sha256:worktree", CommittedHead: "head", DirtyOverlayFingerprint: "sha256:dirty"}, InclusionPolicyIdentity: "sha256:include", ExclusionPolicyIdentity: "sha256:exclude", ParserIdentities: map[string]string{"go": "go/parser@go1.27"}, RecordCount: 1, PostingCount: 2, SourceBindingDigest: "sha256:source-binding", PayloadDigest: "sha256:payload", GenerationIdentity: "sha256:generation", SemanticDigest: "sha256:semantic"}
	counters := WorkCounters{ConsideredRecords: 1, ParsedRepositoryFiles: 1, OpenedRepositoryFiles: 1, ReadRepositoryBytes: 2, ReadStateFiles: 1, WrittenStateFiles: 1}
	changes := ChangeDocument{SchemaVersion: "1", PriorIndexIdentity: "sha256:prior", BeforeRepositoryIdentity: "sha256:before-repo", BeforeWorktreeIdentity: "sha256:before-worktree", BeforeCommittedHead: "before", BeforeDirtyOverlayFingerprint: "sha256:before-dirty", AfterRepositoryIdentity: "sha256:after-repo", AfterWorktreeIdentity: "sha256:after-worktree", AfterCommittedHead: "after", AfterDirtyOverlayFingerprint: "sha256:after-dirty", Level0ChangeManifestIdentity: "sha256:changes", ChangedPaths: []string{"pkg/service.go"}}
	if record.SourceDigest == "" || len(record.SearchTerms) != 2 || manifest.GenerationIdentity == "" || counters.ParsedRepositoryFiles != 1 || changes.PriorIndexIdentity == "" {
		t.Fatalf("normalized types lost required fields: %#v %#v %#v %#v", record, manifest, counters, changes)
	}
}
