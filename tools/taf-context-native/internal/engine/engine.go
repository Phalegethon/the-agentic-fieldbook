// Package engine owns capability-safe dispatch for native Level 1 operations.
package engine

import (
	"context"
	"errors"

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
	ValidateRoots func(wire.Envelope) (boundary.Roots, error)
	Collect       func(boundary.Roots, inventory.Mode) (inventory.Result, error)
	OpenFile      func(*boundary.Roots, string, int64) (boundary.StableFile, error)
	Extract       func(context.Context, boundary.StableFile) ([]model.Record, extract.Report)
	Build         func(context.Context, *boundary.Roots, model.Manifest, []model.Record) (store.Snapshot, error)
	Load          func(context.Context, *boundary.Roots, string) (store.Snapshot, error)
	Inspect       func(context.Context, *boundary.Roots) (store.Status, error)
	ParserIDs     func() map[string]string
}

type Engine struct{ dependencies Dependencies }

func ProductionDependencies() Dependencies {
	registry := extract.NewRegistry()
	return Dependencies{
		ValidateRoots: boundary.ValidateRoots,
		Collect:       inventory.Collect,
		OpenFile: func(roots *boundary.Roots, relative string, maximum int64) (boundary.StableFile, error) {
			return roots.OpenRepositoryFile(relative, maximum)
		},
		Extract:   registry.ExtractContext,
		Build:     store.BuildContext,
		Load:      store.LoadContext,
		Inspect:   store.InspectContext,
		ParserIDs: registry.ParserIdentities,
	}
}

func New(dependencies Dependencies) *Engine { return &Engine{dependencies: dependencies} }

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
	case wire.StatusOperation:
		result, err = engine.state(ctx, &roots, envelope.Request, false)
	case wire.Metrics:
		result, err = engine.state(ctx, &roots, envelope.Request, true)
	case wire.RepositoryMap, wire.SearchSymbols, wire.SearchDocs:
		result, err = engine.query(ctx, &roots, envelope.Request)
	default:
		result = engine.unsupported(envelope.Request)
	}
	if err != nil {
		return wire.Result{}, err
	}
	return render.Fit(envelope.Request, result)
}

func (engine *Engine) ready() bool {
	d := engine.dependencies
	return d.ValidateRoots != nil && d.Collect != nil && d.OpenFile != nil && d.Extract != nil && d.Build != nil && d.Load != nil && d.Inspect != nil && d.ParserIDs != nil
}

func productionLimits() policy.Limits { return policy.ProductionLimits() }
