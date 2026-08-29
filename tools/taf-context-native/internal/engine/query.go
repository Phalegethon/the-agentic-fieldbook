package engine

import (
	"context"
	"errors"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/query"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

// query loads only the immutable generation through the retained state-root
// capability. It deliberately does not call inventory or open repository
// files: indexed records are the sole finding source for Task 8 operations.
func (engine *Engine) query(ctx context.Context, roots *boundary.Roots, request wire.Request) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	if request.IndexIdentity == nil {
		return engine.result(request, wire.Error, "unusable", nil, emptyCoverage(), "build-index"), nil
	}
	if snapshot, cached := engine.cachedSnapshot(request); cached {
		return engine.querySnapshot(ctx, request, snapshot)
	}
	status, err := engine.dependencies.Inspect(ctx, roots)
	if err != nil {
		action := "rebuild-index"
		if errors.Is(err, store.ErrNoCurrent) {
			action = "build-index"
		}
		return engine.result(request, wire.Error, "unusable", request.IndexIdentity, emptyCoverage(), action), nil
	}
	freshness, action := freshnessFor(request, status.Manifest, status.IndexIdentity, engine.dependencies.ParserIDs())
	if freshness != "exact" {
		resultStatus := wire.Stale
		if freshness == "partial" {
			resultStatus = wire.Partial
		}
		return engine.result(request, resultStatus, freshness, request.IndexIdentity, status.Manifest.Coverage, action), nil
	}
	snapshot, err := engine.dependencies.Load(ctx, roots, status.IndexIdentity)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return wire.Result{}, err
		}
		if errors.Is(err, store.ErrIndexMismatch) {
			return engine.result(request, wire.Stale, "structurally-stale", request.IndexIdentity, status.Manifest.Coverage, "rebuild-index"), nil
		}
		action := "rebuild-index"
		if errors.Is(err, store.ErrNoCurrent) {
			action = "build-index"
		}
		return engine.result(request, wire.Error, "unusable", request.IndexIdentity, emptyCoverage(), action), nil
	}
	// Load rechecks CURRENT after materialization. Re-evaluate the returned
	// immutable manifest as well, so an injected or future alternate loader
	// cannot turn an Inspect/Load race into exact evidence.
	loadedFreshness, loadedAction := freshnessFor(request, snapshot.Manifest, snapshot.IndexIdentity, engine.dependencies.ParserIDs())
	if loadedFreshness != "exact" || snapshot.IndexIdentity != status.IndexIdentity {
		resultStatus := wire.Stale
		if loadedFreshness == "partial" {
			resultStatus = wire.Partial
		}
		if snapshot.IndexIdentity != status.IndexIdentity {
			loadedFreshness, loadedAction = "structurally-stale", "rebuild-index"
		}
		return engine.result(request, resultStatus, loadedFreshness, request.IndexIdentity, snapshot.Manifest.Coverage, loadedAction), nil
	}
	engine.rememberSnapshot(snapshot)
	return engine.querySnapshot(ctx, request, snapshot)
}

func (engine *Engine) querySnapshot(ctx context.Context, request wire.Request, snapshot store.Snapshot) (wire.Result, error) {
	var response query.Response
	switch request.Operation {
	case wire.RepositoryMap:
		response = query.RepositoryMap(snapshot, request, productionLimits())
	case wire.SearchSymbols, wire.SearchDocs:
		response = query.Search(snapshot, request, productionLimits())
	default:
		return engine.unsupported(request), nil
	}
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	resultStatus, nextAction := wire.Ready, "use-index"
	if response.Partial {
		resultStatus, nextAction = wire.Partial, "refine-query"
	}
	result := engine.result(request, resultStatus, "exact", request.IndexIdentity, snapshot.Manifest.Coverage, nextAction)
	result.Findings = findings(response.Records)
	result.OmittedCount = response.Omitted
	if response.Partial {
		result.Warnings = []string{"query-frontier-exhausted"}
	}
	return result, nil
}

func findings(records []model.Record) []wire.Finding {
	output := make([]wire.Finding, len(records))
	for index, record := range records {
		output[index] = wire.Finding{Rank: index + 1, ResultIdentity: record.Identity, Path: record.Path, StartLine: record.StartLine, EndLine: record.EndLine, Language: record.Language, RecordKind: string(record.RecordKind), SourceType: record.SourceType, QualifiedName: record.QualifiedName, ExtractionMethod: record.ExtractionMethod, EvidenceClass: string(record.EvidenceClass), Preview: record.Preview}
	}
	return output
}
