package engine

import (
	"bytes"
	"context"
	"errors"
	"path"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

const (
	maximumSnippetPreviewCharacters = 12000
	maximumSnippetPreviewBytes      = maximumSnippetPreviewCharacters * utf8.UTFMax
	maximumSnippetMetadataBytes     = 512
)

var errSnippetUnverifiable = errors.New("unverifiable indexed source snippet")
var errSnippetPreviewTooLarge = errors.New("indexed source preview exceeds bounded output field")

type snippetGroup struct {
	path    string
	digest  string
	maximum int64
	items   []snippetItem
}

type snippetItem struct {
	position int
	record   model.Record
}

// sourceSnippets reopens only requested, identity-bound source paths through
// the retained Roots capability. It never uses indexed previews as evidence.
func (engine *Engine) sourceSnippets(ctx context.Context, roots *boundary.Roots, request wire.Request) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	if request.IndexIdentity == nil {
		return engine.result(request, wire.Error, "unusable", nil, emptyCoverage(), "build-index"), nil
	}
	snapshot, cached := engine.cachedSnapshot(request)
	status := store.Status{}
	if cached {
		current, currentErr := engine.dependencies.CurrentGeneration(ctx, roots)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		if currentErr != nil || current != snapshot.Manifest.GenerationIdentity || !validSnippetIdentity(current) {
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
	} else {
		var err error
		status, err = engine.dependencies.Peek(ctx, roots)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		if err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return wire.Result{}, err
			}
			return engine.snippetUnavailable(request, err), nil
		}
		if freshness, _ := freshnessFor(request, status.Manifest, status.IndexIdentity, engine.dependencies.ParserIDs()); freshness != "exact" {
			return engine.snippetStale(request, status.Manifest.Coverage), nil
		}
		if !validSnippetIdentity(status.GenerationIdentity) || status.GenerationIdentity != status.Manifest.GenerationIdentity {
			return engine.snippetStale(request, status.Manifest.Coverage), nil
		}
		snapshot, err = engine.dependencies.Load(ctx, roots, status.IndexIdentity)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		if err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return wire.Result{}, err
			}
			if errors.Is(err, store.ErrIndexMismatch) {
				return engine.snippetStale(request, status.Manifest.Coverage), nil
			}
			return engine.snippetUnavailable(request, err), nil
		}
		if snapshot.IndexIdentity != status.IndexIdentity || !validSnippetIdentity(snapshot.Manifest.GenerationIdentity) || snapshot.Manifest.GenerationIdentity != status.GenerationIdentity {
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
		if freshness, _ := freshnessFor(request, snapshot.Manifest, snapshot.IndexIdentity, engine.dependencies.ParserIDs()); freshness != "exact" {
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
		engine.rememberSnapshot(snapshot)
	}

	groups, err := resolveSnippetGroups(snapshot.Records, request.ResultIdentities)
	if err != nil {
		return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
	}
	findings := make([]wire.Finding, len(request.ResultIdentities))
	keep := make([]bool, len(request.ResultIdentities))
	for _, group := range groups {
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if err := engine.readSnippetGroup(ctx, roots, group, findings, keep); err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return wire.Result{}, err
			}
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
	}
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	// A second current check closes state-selection races across all repository
	// reads. Cached immutable bytes were already validated before retention.
	if cached {
		current, currentErr := engine.dependencies.CurrentGeneration(ctx, roots)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		if currentErr != nil || current != snapshot.Manifest.GenerationIdentity {
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
	} else {
		after, err := engine.dependencies.Peek(ctx, roots)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		if err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return wire.Result{}, err
			}
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
		if err := ctx.Err(); err != nil {
			return wire.Result{}, err
		}
		if after.IndexIdentity != status.IndexIdentity || !validSnippetIdentity(after.GenerationIdentity) || after.GenerationIdentity != status.GenerationIdentity || after.Manifest.GenerationIdentity != status.GenerationIdentity {
			return engine.snippetStale(request, after.Manifest.Coverage), nil
		}
		if freshness, _ := freshnessFor(request, after.Manifest, after.IndexIdentity, engine.dependencies.ParserIDs()); freshness != "exact" {
			return engine.snippetStale(request, after.Manifest.Coverage), nil
		}
	}

	result := engine.result(request, wire.Ready, "exact", request.IndexIdentity, snapshot.Manifest.Coverage, "use-index")
	prefix := true
	for index, finding := range findings {
		if prefix && keep[index] {
			result.Findings = append(result.Findings, finding)
		} else {
			prefix = false
			result.OmittedCount++
		}
	}
	if result.OmittedCount != 0 {
		result.Status = wire.Partial
		result.NextSafeAction = "refine-query"
	}
	return result, nil
}

func (engine *Engine) readSnippetGroup(ctx context.Context, roots *boundary.Roots, group snippetGroup, findings []wire.Finding, keep []bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	file, err := engine.dependencies.OpenFile(roots, group.path, group.maximum)
	if contextErr := ctx.Err(); contextErr != nil {
		return contextErr
	}
	if err != nil {
		return errSnippetUnverifiable
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if file.RelativePath != group.path || file.Size != int64(len(file.Bytes)) || "sha256:"+file.SHA256 != group.digest {
		return errSnippetUnverifiable
	}
	for _, item := range group.items {
		preview, err := indexedLinePreview(file.Bytes, item.record.StartLine, item.record.EndLine)
		if err != nil {
			if errors.Is(err, errSnippetPreviewTooLarge) {
				continue
			}
			return errSnippetUnverifiable
		}
		findings[item.position] = snippetFinding(item.record, item.position+1, preview)
		keep[item.position] = true
	}
	return nil
}

func resolveSnippetGroups(records []model.Record, identities []string) ([]snippetGroup, error) {
	groups := make(map[string]*snippetGroup, len(identities))
	for position, identity := range identities {
		index := sort.Search(len(records), func(index int) bool { return records[index].Identity >= identity })
		if index == len(records) || records[index].Identity != identity || (index > 0 && records[index-1].Identity == identity) || (index+1 < len(records) && records[index+1].Identity == identity) {
			return nil, errSnippetUnverifiable
		}
		record := records[index]
		maximum, ok := validSnippetRecord(record)
		if !ok {
			return nil, errSnippetUnverifiable
		}
		group := groups[record.Path]
		if group == nil {
			group = &snippetGroup{path: record.Path, digest: record.SourceDigest, maximum: maximum}
			groups[record.Path] = group
		} else if group.digest != record.SourceDigest || group.maximum != maximum {
			return nil, errSnippetUnverifiable
		}
		group.items = append(group.items, snippetItem{position: position, record: record})
	}
	output := make([]snippetGroup, 0, len(groups))
	for _, group := range groups {
		output = append(output, *group)
	}
	sort.Slice(output, func(i, j int) bool { return output[i].path < output[j].path })
	return output, nil
}

func validSnippetRecord(record model.Record) (int64, bool) {
	if !validSnippetIdentity(record.Identity) || !validSnippetPath(record.Path) || record.StartLine < 1 || record.EndLine < record.StartLine || !validSnippetText(record.Language, false) || !validSnippetText(record.QualifiedName, true) || !validSnippetText(record.ExtractionMethod, false) || record.EvidenceClass != model.Verified || !validSnippetIdentity(record.SourceDigest) {
		return 0, false
	}
	if record.RecordKind != model.Module && record.RecordKind != model.Definition && record.RecordKind != model.Import && record.RecordKind != model.EntryPoint && record.RecordKind != model.Configuration && record.RecordKind != model.Heading && record.RecordKind != model.DocumentChunk {
		return 0, false
	}
	switch record.SourceType {
	case "source", "configuration":
		return int64(productionLimits().MaximumSourceFileBytes), true
	case "document":
		return int64(productionLimits().MaximumMarkdownFileBytes), true
	default:
		return 0, false
	}
}

func validSnippetIdentity(value string) bool {
	if len(value) != len("sha256:")+64 || !strings.HasPrefix(value, "sha256:") {
		return false
	}
	for _, character := range value[len("sha256:"):] {
		if !(character >= '0' && character <= '9') && !(character >= 'a' && character <= 'f') {
			return false
		}
	}
	return true
}

func validSnippetPath(value string) bool {
	return validSnippetText(value, false) && !strings.Contains(value, `\`) && !strings.HasPrefix(value, "/") && path.Clean(value) == value && value != "." && !strings.HasPrefix(value, "../") && !strings.Contains(value, "/../")
}

func validSnippetText(value string, empty bool) bool {
	return (empty || value != "") && len(value) <= maximumSnippetMetadataBytes && utf8.ValidString(value) && !strings.ContainsAny(value, "\x00\n\r")
}

func indexedLinePreview(contents []byte, start, end int) (string, error) {
	if start < 1 || end < start {
		return "", errSnippetUnverifiable
	}
	line, offset, selected := 1, 0, make([]string, 0)
	previewBytes, previewCharacters := 0, 0
	for offset < len(contents) {
		next := offset
		for next < len(contents) && contents[next] != '\n' {
			next++
		}
		if line >= start && line <= end {
			value := contents[offset:next]
			if len(value) > 0 && value[len(value)-1] == '\r' {
				value = value[:len(value)-1]
			}
			if bytes.IndexByte(value, '\r') >= 0 || bytes.IndexByte(value, 0) >= 0 || !utf8.Valid(value) {
				return "", errSnippetUnverifiable
			}
			additionalBytes, additionalCharacters := len(value), utf8.RuneCount(value)
			if len(selected) != 0 {
				additionalBytes++
				additionalCharacters++
			}
			if additionalBytes > maximumSnippetPreviewBytes-previewBytes || additionalCharacters > maximumSnippetPreviewCharacters-previewCharacters {
				return "", errSnippetPreviewTooLarge
			}
			previewBytes += additionalBytes
			previewCharacters += additionalCharacters
			selected = append(selected, string(value))
		}
		if next == len(contents) {
			break
		}
		offset, line = next+1, line+1
	}
	if len(contents) == 0 || start > line || end > line || len(selected) != end-start+1 {
		return "", errSnippetUnverifiable
	}
	return strings.Join(selected, "\n"), nil
}

func snippetFinding(record model.Record, rank int, preview string) wire.Finding {
	return wire.Finding{Rank: rank, ResultIdentity: record.Identity, Path: record.Path, StartLine: record.StartLine, EndLine: record.EndLine, Language: record.Language, RecordKind: string(record.RecordKind), SourceType: record.SourceType, QualifiedName: record.QualifiedName, ExtractionMethod: record.ExtractionMethod, EvidenceClass: string(record.EvidenceClass), Preview: preview}
}

func (engine *Engine) snippetStale(request wire.Request, coverage model.Coverage) wire.Result {
	return engine.result(request, wire.Stale, "structurally-stale", request.IndexIdentity, coverage, "update-index")
}

func (engine *Engine) snippetUnavailable(request wire.Request, err error) wire.Result {
	action := "rebuild-index"
	if errors.Is(err, store.ErrNoCurrent) {
		action = "build-index"
	}
	return engine.result(request, wire.Error, "unusable", request.IndexIdentity, emptyCoverage(), action)
}
