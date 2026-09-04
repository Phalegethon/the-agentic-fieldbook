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
	status, err := engine.dependencies.Peek(ctx, roots)
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
	// cannot turn a Peek/Load race into exact evidence.
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
	var (
		selected      []wire.Finding
		omitted       int
		partial       bool
		unindexed     bool
		groups        *[]wire.OverviewGroup
		overview      *wire.OverviewSummary
		extraPrefixes bool
		rootUnmatched bool
	)
	switch request.Operation {
	case wire.RepositoryMap:
		response := query.RepositoryMap(snapshot, request, productionLimits())
		selected, omitted, partial = findings(response.Records), response.Omitted, response.Partial
	case wire.SearchSymbols, wire.SearchDocs:
		response := query.Search(snapshot, request, productionLimits())
		selected, omitted, partial = findings(response.Records), response.Omitted, response.Partial
	case wire.RelatedSymbols:
		response := query.Related(snapshot, request, productionLimits())
		// An anchor that is not a record a relationship may start from is
		// refused the way an unusable snippet identity is: no findings, and an
		// index refresh as the next safe action.
		if response.Unknown {
			return engine.snippetStale(request, snapshot.Manifest.Coverage), nil
		}
		selected, omitted, partial = relatedFindings(response.Findings), response.Omitted, response.Partial
	case wire.ChangedSymbols:
		response := query.Changed(snapshot, request, productionLimits())
		selected, omitted, partial = findings(response.Records), response.Omitted, response.Partial
		// A changed path the index carries no record for is neither a finding
		// nor a counted omission; it is reported once, below, so the caller
		// learns the change set reached further than the index.
		unindexed = response.Unindexed
	case wire.RepositoryOverview:
		response := query.Overview(snapshot, request, productionLimits())
		selected, omitted, partial = findings(response.Records), response.Omitted, response.Partial
		// The group table and the summary are the answer's first layer, so they
		// travel next to the findings rather than instead of them.
		groups, overview = &response.Groups, &response.Overview
		// A request that named more than one path prefix was served from the
		// first of them, which is reported once, below.
		extraPrefixes = response.ExtraPathPrefixes
		// A root no indexed path lies under counts nothing, and only this
		// warning tells that apart from a directory holding nothing counted.
		rootUnmatched = response.RootUnmatched
	default:
		return engine.unsupported(request), nil
	}
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	resultStatus, nextAction := wire.Ready, "use-index"
	if !completeCoverage(snapshot.Manifest.Coverage, false) {
		resultStatus = wire.Partial
	}
	if partial {
		resultStatus, nextAction = wire.Partial, "refine-query"
	}
	result := engine.result(request, resultStatus, "exact", request.IndexIdentity, snapshot.Manifest.Coverage, nextAction)
	result.Findings = selected
	result.OmittedCount = omitted
	// Truncated is the honest flag: the finding list is known to be incomplete
	// because the ranking overflowed, the renderer trimmed, or a budget stopped
	// the search. Omissions the engine could not count are never estimated.
	result.Truncated = partial || omitted > 0
	if groups != nil {
		result.Groups, result.Overview = groups, overview
	}
	result.Warnings = sourceCatalogWarnings(snapshot.Manifest.SourceCatalog)
	if partial {
		result.Warnings = appendBoundedWarnings(result.Warnings, "query-frontier-exhausted")
	}
	if unindexed {
		result.Warnings = appendBoundedWarnings(result.Warnings, "changed-path-not-indexed")
	}
	if extraPrefixes {
		result.Warnings = appendBoundedWarnings(result.Warnings, "overview-root-first-prefix")
	}
	if rootUnmatched {
		result.Warnings = appendBoundedWarnings(result.Warnings, "overview-root-not-a-directory")
	}
	return result, nil
}

// relatedFindings carries the four schema-2 edge fields next to the record the
// resolution reached, so a reader sees both what was found and how well the
// edge that led there is evidenced.
func relatedFindings(edges []query.RelatedFinding) []wire.Finding {
	records := make([]model.Record, len(edges))
	for index, edge := range edges {
		records[index] = edge.Record
	}
	output := findings(records)
	for index, edge := range edges {
		output[index].Relation = edge.Relation
		output[index].EdgeEvidence = string(edge.EdgeEvidence)
		output[index].ReferenceLine = edge.ReferenceLine
		output[index].ReferenceCount = edge.ReferenceCount
	}
	return output
}

func findings(records []model.Record) []wire.Finding {
	output := make([]wire.Finding, len(records))
	for index, record := range records {
		output[index] = wire.Finding{Rank: index + 1, ResultIdentity: record.Identity, Path: record.Path, StartLine: record.StartLine, EndLine: record.EndLine, Language: record.Language, RecordKind: string(record.RecordKind), SourceType: record.SourceType, QualifiedName: record.QualifiedName, ExtractionMethod: record.ExtractionMethod, EvidenceClass: string(record.EvidenceClass), Preview: record.Preview}
	}
	return output
}
