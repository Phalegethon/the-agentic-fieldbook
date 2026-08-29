// Package render turns deterministic domain results into bounded wire results.
package render

import (
	"bytes"
	"errors"
	"sort"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

var ErrUnrenderable = errors.New("mandatory Level 1 result exceeds output budget")

// Fit copies and normalizes result, retaining only a deterministic prefix of
// optional findings that can be encoded within both frozen output limits.
func Fit(request wire.Request, result wire.Result) (wire.Result, error) {
	output := cloneResult(result)
	normalize(&output, request.MaximumResults)
	for {
		output.OutputCharacters = wire.OutputCharacters(output)
		var encoded bytes.Buffer
		if err := wire.EncodeResult(&encoded, output); err != nil {
			return wire.Result{}, err
		}
		if output.OutputCharacters <= request.MaximumModelOutputCharacters && encoded.Len() <= policy.ProductionLimits().MaximumStdoutBytes {
			return output, nil
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
	result.Truncated = result.OmittedCount > 0
}
