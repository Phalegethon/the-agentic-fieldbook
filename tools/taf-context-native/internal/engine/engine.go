// Package engine owns capability-safe dispatch for native Level 1 operations.
package engine

import (
	"context"
	"errors"
	"sync"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/extract"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/inventory"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/render"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/store"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

var ErrDependencies = errors.New("incomplete native Level 1 dependencies")

type Dependencies struct {
	ValidateRoots          func(wire.Envelope) (boundary.Roots, error)
	Collect                func(boundary.Roots, inventory.Mode) (inventory.Result, error)
	OpenFile               func(*boundary.Roots, string, int64) (boundary.StableFile, error)
	OpenControl            func(*boundary.Roots, string, int64) (boundary.StableFile, error)
	Extract                func(context.Context, boundary.StableFile) ([]model.Record, extract.Report)
	Build                  func(context.Context, *boundary.Roots, model.Manifest, []model.Record) (store.Snapshot, error)
	BuildWithBarrier       func(context.Context, *boundary.Roots, model.Manifest, []model.Record, func() error) (store.Snapshot, error)
	BuildCachedWithBarrier func(context.Context, *boundary.Roots, store.Snapshot, model.Manifest, []model.Record, func() error) (store.Snapshot, error)
	Load                   func(context.Context, *boundary.Roots, string) (store.Snapshot, error)
	Inspect                func(context.Context, *boundary.Roots) (store.Status, error)
	// Peek verifies CURRENT, the manifest, the READY marker, the payload
	// digest, and the generation identity without the raw structural
	// validator. Query, snippet, and status paths use it; metrics and update
	// keep Inspect.
	Peek              func(context.Context, *boundary.Roots) (store.Status, error)
	CurrentGeneration func(context.Context, *boundary.Roots) (string, error)
	ParserIDs         func() map[string]string
	Fit               func(context.Context, wire.Request, wire.Result) (wire.Result, error)
	// ObserveUpdateCounters is intentionally an in-process-only test/evaluation
	// seam. Production leaves it nil; no high-cardinality data crosses wire.
	ObserveUpdateCounters func(model.WorkCounters)
}

type snapshotCache struct {
	mu       sync.RWMutex
	snapshot store.Snapshot
	ready    bool
}

type Engine struct {
	dependencies Dependencies
	cache        *snapshotCache
}

func ProductionDependencies() Dependencies {
	registry := extract.NewRegistry()
	return Dependencies{
		ValidateRoots: boundary.ValidateRoots,
		Collect:       inventory.Collect,
		OpenFile: func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
			return roots.OpenRepositoryFile(relative, maximum)
		},
		OpenControl: func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
			return roots.OpenStateControlFile(relative, maximum)
		},
		Extract:                registry.ExtractContext,
		Build:                  store.BuildContext,
		BuildWithBarrier:       store.BuildContextWithBarrier,
		BuildCachedWithBarrier: store.BuildCachedContextWithBarrier,
		Load:                   store.LoadContext,
		Inspect:                store.InspectContext,
		Peek:                   store.PeekContext,
		CurrentGeneration:      store.CurrentGenerationContext,
		ParserIDs:              registry.ParserIdentities,
		Fit:                    render.FitContext,
	}
}

func New(dependencies Dependencies) *Engine { return &Engine{dependencies: dependencies} }

// NewCached retains one already-validated immutable generation for a bounded
// multi-request process. New remains uncached so one-shot callers and fault
// tests preserve their per-request state validation semantics.
func NewCached(dependencies Dependencies) *Engine {
	return &Engine{dependencies: dependencies, cache: &snapshotCache{}}
}

func (engine *Engine) cachedSnapshot(request wire.Request) (store.Snapshot, bool) {
	if engine == nil || engine.cache == nil || request.IndexIdentity == nil {
		return store.Snapshot{}, false
	}
	engine.cache.mu.RLock()
	defer engine.cache.mu.RUnlock()
	snapshot := engine.cache.snapshot
	if !engine.cache.ready || snapshot.IndexIdentity != *request.IndexIdentity {
		return store.Snapshot{}, false
	}
	freshness, _ := freshnessFor(request, snapshot.Manifest, snapshot.IndexIdentity, engine.dependencies.ParserIDs())
	if freshness != "exact" {
		return store.Snapshot{}, false
	}
	return snapshot, true
}

func (engine *Engine) cachedIndex(identity string) (store.Snapshot, bool) {
	if engine == nil || engine.cache == nil || identity == "" {
		return store.Snapshot{}, false
	}
	engine.cache.mu.RLock()
	defer engine.cache.mu.RUnlock()
	if !engine.cache.ready || engine.cache.snapshot.IndexIdentity != identity {
		return store.Snapshot{}, false
	}
	return engine.cache.snapshot, true
}

func (engine *Engine) rememberSnapshot(snapshot store.Snapshot) {
	if engine == nil || engine.cache == nil || snapshot.IndexIdentity == "" {
		return
	}
	engine.cache.mu.Lock()
	engine.cache.snapshot = snapshot
	engine.cache.ready = true
	engine.cache.mu.Unlock()
}

func (engine *Engine) Execute(ctx context.Context, envelope wire.Envelope) (wire.Result, error) {
	if engine == nil || !engine.ready() {
		return wire.Result{}, ErrDependencies
	}
	if err := wire.ValidateRequest(envelope.Request); err != nil {
		return wire.Result{}, err
	}
	roots, err := engine.dependencies.ValidateRoots(envelope)
	if err != nil {
		return wire.Result{}, err
	}
	defer roots.Close()
	var result wire.Result
	switch envelope.Request.Operation {
	case wire.Estimate:
		result, err = engine.estimate(ctx, &roots, envelope.Request)
	case wire.Build:
		result, err = engine.build(ctx, &roots, envelope.Request)
	case wire.Update:
		result, err = engine.update(ctx, &roots, envelope.Request, envelope.ChangedPathsDocument)
	case wire.StatusOperation:
		result, err = engine.state(ctx, &roots, envelope.Request, false)
	case wire.Metrics:
		result, err = engine.state(ctx, &roots, envelope.Request, true)
	case wire.RepositoryMap, wire.SearchSymbols, wire.SearchDocs:
		result, err = engine.query(ctx, &roots, envelope.Request)
	case wire.SourceSnippets:
		result, err = engine.sourceSnippets(ctx, &roots, envelope.Request)
	default:
		result = engine.unsupported(envelope.Request)
	}
	if err != nil {
		return wire.Result{}, err
	}
	publishedUpdate := envelope.Request.Operation == wire.Update && result.Status == wire.Ready
	if !publishedUpdate {
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
	}
	fitContext := ctx
	if publishedUpdate {
		fitContext = context.WithoutCancel(ctx)
	}
	fitted, err := engine.dependencies.Fit(fitContext, envelope.Request, result)
	if !publishedUpdate {
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
	}
	if err != nil {
		if publishedUpdate {
			// CURRENT already names the new immutable generation. A renderer seam
			// must therefore not turn that completed publication into an error (or
			// emit an internally inconsistent ready result). Update responses have
			// no optional findings; their bounded fallback only needs the frozen
			// character accounting restored before the caller serializes it.
			fallback := result
			fallback.Warnings = append([]string{}, result.Warnings...)
			fallback.OutputCharacters = wire.OutputCharacters(fallback)
			return fallback, nil
		}
		return wire.Result{}, err
	}
	// A snippet response that the shared renderer had to shorten is partial
	// evidence, even when every source reread itself verified. Refit after the
	// status/action change so counters and final transport accounting agree.
	if envelope.Request.Operation == wire.SourceSnippets && result.Status == wire.Ready && fitted.OmittedCount > result.OmittedCount {
		result.Status = wire.Partial
		result.NextSafeAction = "refine-query"
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		refitted, refitErr := engine.dependencies.Fit(ctx, envelope.Request, result)
		if contextErr := ctx.Err(); contextErr != nil {
			return wire.Result{}, contextErr
		}
		return refitted, refitErr
	}
	return fitted, nil
}

func (engine *Engine) observeUpdateCounters(counters model.WorkCounters) {
	if engine.dependencies.ObserveUpdateCounters != nil {
		engine.dependencies.ObserveUpdateCounters(counters)
	}
}

func (engine *Engine) ready() bool {
	d := engine.dependencies
	return d.ValidateRoots != nil && d.Collect != nil && d.OpenFile != nil && d.OpenControl != nil && d.Extract != nil && d.Build != nil && d.BuildWithBarrier != nil && d.BuildCachedWithBarrier != nil && d.Load != nil && d.Inspect != nil && d.Peek != nil && d.CurrentGeneration != nil && d.ParserIDs != nil && d.Fit != nil
}

func productionLimits() policy.Limits { return policy.ProductionLimits() }
