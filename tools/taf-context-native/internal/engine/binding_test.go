package engine

import (
	"testing"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
)

func TestSemanticBindingIncludesEveryQueryAndEvidenceField(t *testing.T) {
	base := model.Record{Identity: engineSHA, Path: "a.go", StartLine: 1, EndLine: 2, Language: "go", RecordKind: model.Definition, SourceType: "source", QualifiedName: "A", ExtractionMethod: "go-ast", EvidenceClass: model.Verified, SearchTerms: []string{"a", "b"}, SourceDigest: engineSHA, Preview: "p"}
	want := semanticBinding([]model.Record{base})
	mutations := []func(*model.Record){func(r *model.Record) { r.Identity = testSHA2 }, func(r *model.Record) { r.Path = "b.go" }, func(r *model.Record) { r.StartLine = 2 }, func(r *model.Record) { r.EndLine = 3 }, func(r *model.Record) { r.Language = "rust" }, func(r *model.Record) { r.SourceType = "document" }, func(r *model.Record) { r.QualifiedName = "B" }, func(r *model.Record) { r.ExtractionMethod = "other" }, func(r *model.Record) { r.EvidenceClass = model.Inferred }, func(r *model.Record) { r.SearchTerms = []string{"b", "a"} }, func(r *model.Record) { r.SourceDigest = testSHA2 }, func(r *model.Record) { r.Preview = "q" }}
	for _, mutate := range mutations {
		got := base
		mutate(&got)
		if semanticBinding([]model.Record{got}) == want {
			t.Fatal("semantic field mutation was not bound")
		}
	}
}

const testSHA2 = "sha256:bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a"
