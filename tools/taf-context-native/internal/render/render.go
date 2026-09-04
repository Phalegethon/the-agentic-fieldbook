// Package render turns deterministic domain results into bounded wire results.
package render

import (
	"bytes"
	"context"
	"errors"
	"sort"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

var ErrUnrenderable = errors.New("mandatory Level 1 result exceeds output budget")

// FitContext adds cancellation dominance around the deterministic renderer;
// Fit remains the compatibility entry point for context-free callers.
func FitContext(ctx context.Context, request wire.Request, result wire.Result) (wire.Result, error) {
	if err := ctx.Err(); err != nil {
		return wire.Result{}, err
	}
	fitted, err := Fit(request, result)
	if contextErr := ctx.Err(); contextErr != nil {
		return wire.Result{}, contextErr
	}
	return fitted, err
}

// Fit copies and normalizes result, retaining only a deterministic prefix of
// optional findings that can be encoded within both frozen output limits.
func Fit(request wire.Request, result wire.Result) (wire.Result, error) {
	output := cloneResult(result)
	normalize(&output, request.MaximumResults)
	// A table with more rows than the wire admits is folded down to the bound
	// before anything is measured, so a pathological table meets the fold
	// rather than a validation error it cannot recover from. Every fold trades
	// one directory row for a place in the "*" row, so reaching a bound of n
	// rows from m costs m-n+1 of them.
	if output.Groups != nil && len(*output.Groups) > wire.MaximumOverviewGroups {
		foldOverviewRows(&output, len(*output.Groups)-wire.MaximumOverviewGroups+1)
	}
	for {
		output.OutputCharacters = wire.OutputCharacters(output)
		encodedBytes, _, err := wire.MeasureResult(output)
		if err != nil {
			return wire.Result{}, err
		}
		if output.OutputCharacters <= request.MaximumModelOutputCharacters && encodedBytes <= policy.ProductionLimits().MaximumStdoutBytes {
			var final bytes.Buffer
			if err := wire.EncodeResult(&final, output); err != nil {
				return wire.Result{}, err
			}
			return output, nil
		}
		// A directory table can be wider than any transport frame carries, so
		// it is what pays first: the counts stay in the table and only the
		// detail behind them goes, and nothing the caller asked for is
		// dropped while a row can still be folded. The caller's own output
		// budget is a different rule and stays the caller's — this fold is
		// the transport's, and it is what keeps a wide table an answer.
		if excess := encodedBytes - policy.ProductionLimits().MaximumStdoutBytes; excess > 0 {
			if foldOverviewRows(&output, overviewFoldEstimate(output, encodedBytes, excess)) {
				continue
			}
		}
		if len(output.Findings) == 0 {
			return wire.Result{}, ErrUnrenderable
		}
		output.Findings = output.Findings[:len(output.Findings)-1]
		output.OmittedCount++
		output.Truncated = true
		rewriteRanksAndCounts(&output)
	}
}

func cloneResult(input wire.Result) wire.Result {
	output := input
	output.Findings = make([]wire.Finding, len(input.Findings))
	copy(output.Findings, input.Findings)
	output.Warnings = make([]string, len(input.Warnings))
	copy(output.Warnings, input.Warnings)
	output.ParserVersions = make(map[string]string, len(input.ParserVersions))
	for key, value := range input.ParserVersions {
		output.ParserVersions[key] = value
	}
	output.Coverage.ExclusionReasonCounts = make(map[string]int, len(input.Coverage.ExclusionReasonCounts))
	for key, value := range input.Coverage.ExclusionReasonCounts {
		output.Coverage.ExclusionReasonCounts[key] = value
	}
	return output
}

func normalize(result *wire.Result, maximumResults int) {
	sort.SliceStable(result.Findings, func(i, j int) bool {
		if result.Findings[i].Rank != result.Findings[j].Rank {
			return result.Findings[i].Rank < result.Findings[j].Rank
		}
		return result.Findings[i].ResultIdentity < result.Findings[j].ResultIdentity
	})
	if len(result.Findings) > maximumResults {
		result.OmittedCount += len(result.Findings) - maximumResults
		result.Findings = result.Findings[:maximumResults]
	}
	sort.Strings(result.Warnings)
	warnings := result.Warnings[:0]
	for _, warning := range result.Warnings {
		if len(warnings) == 0 || warnings[len(warnings)-1] != warning {
			warnings = append(warnings, warning)
		}
	}
	result.Warnings = warnings
	rewriteRanksAndCounts(result)
}

func rewriteRanksAndCounts(result *wire.Result) {
	for index := range result.Findings {
		result.Findings[index].Rank = index + 1
	}
	result.ReturnedCount = len(result.Findings)
	// Never clear a truncation the engine already reported; only raise it.
	result.Truncated = result.Truncated || result.OmittedCount > 0
}

// foldOverviewRows folds count rows and reports whether it folded any. Folding
// stops early at the floor of one directory row, so a caller may ask for more
// rows than the table can still give.
func foldOverviewRows(result *wire.Result, count int) bool {
	folded := false
	for index := 0; index < count && foldOverviewTail(result); index++ {
		folded = true
	}
	return folded
}

// overviewFoldEstimate answers how many rows a table has to lose for its
// result to fit a frame it exceeds by excess bytes. The rows of a table are of
// a kind — one prefix, seven counters and a short language list each — so the
// table's own average is a fair price per row, and it is an over-estimate of
// that price because the result carries more than its table: the estimate
// therefore lands short of the rows needed rather than beyond them, and the
// fitting loop simply measures again. Estimating is what keeps a table of
// thousands of rows from costing one full re-encode per row folded.
func overviewFoldEstimate(result wire.Result, encodedBytes, excess int) int {
	if result.Groups == nil || encodedBytes <= 0 {
		return 1
	}
	if rows := len(*result.Groups); rows > 0 {
		return max(1, excess*rows/encodedBytes)
	}
	return 1
}

// foldOverviewTail folds the table's last directory row into the "*" row and
// reports whether it could. The folded row speaks for every directory the
// answer had no room for, so there is exactly one of it, it is always last,
// and a "*" row already present is merged into rather than doubled. It stands
// for whole directories instead of one of them, which is why its depth is zero
// and it names no representative file. other_group_count counts every row the
// table lost, so a reader can still add the repository up. One directory row
// is the floor — a table of nothing but "*" would describe no repository at
// all — and reaching it is what false reports. The result handed in is left
// alone: the fold rebuilds the table and the summary rather than writing
// through the pointers it was given.
func foldOverviewTail(result *wire.Result) bool {
	if result.Groups == nil || result.Overview == nil {
		return false
	}
	groups := append([]wire.OverviewGroup(nil), (*result.Groups)...)
	merged := wire.OverviewGroup{PathPrefix: overviewFoldedPrefix}
	lists := make([][]wire.OverviewLanguage, 0, 2)
	if last := len(groups) - 1; last >= 0 && groups[last].PathPrefix == overviewFoldedPrefix {
		merged = addOverviewCounts(merged, groups[last])
		lists = append(lists, groups[last].Languages)
		groups = groups[:last]
	}
	if len(groups) < 2 {
		return false
	}
	tail := groups[len(groups)-1]
	groups = groups[:len(groups)-1]
	merged = addOverviewCounts(merged, tail)
	merged.Languages = mergeOverviewLanguages(append(lists, tail.Languages)...)
	summary := *result.Overview
	summary.OtherGroupCount++
	groups = append(groups, merged)
	result.Groups, result.Overview = &groups, &summary
	return true
}

// overviewFoldedPrefix is the path prefix of the row that stands for the
// directories a table had no room for. The engine builds no such row of its
// own; only a fold does.
const overviewFoldedPrefix = "*"

func addOverviewCounts(row wire.OverviewGroup, added wire.OverviewGroup) wire.OverviewGroup {
	row.FileCount += added.FileCount
	row.DefinitionCount += added.DefinitionCount
	row.EntryPointCount += added.EntryPointCount
	row.DocumentCount += added.DocumentCount
	row.ConfigurationCount += added.ConfigurationCount
	return row
}

// mergeOverviewLanguages sums the language counts of the rows a fold joins,
// most files first and ties by name — the order a single row's list already
// promises, so the folded row reads like every other row of the table. The
// merged list is bounded the way every language list is; a fold joining more
// languages than a result may carry keeps the ones most of its files are
// written in.
func mergeOverviewLanguages(lists ...[]wire.OverviewLanguage) []wire.OverviewLanguage {
	merged := make([]wire.OverviewLanguage, 0, 8)
	for _, list := range lists {
		for _, language := range list {
			position := -1
			for index := range merged {
				if merged[index].Language == language.Language {
					position = index
					break
				}
			}
			if position < 0 {
				merged = append(merged, language)
				continue
			}
			merged[position].FileCount += language.FileCount
		}
	}
	sort.Slice(merged, func(left, right int) bool {
		if merged[left].FileCount != merged[right].FileCount {
			return merged[left].FileCount > merged[right].FileCount
		}
		return merged[left].Language < merged[right].Language
	})
	if maximum := policy.ProductionLimits().MaximumCollectionItems; len(merged) > maximum {
		merged = merged[:maximum]
	}
	return merged
}
