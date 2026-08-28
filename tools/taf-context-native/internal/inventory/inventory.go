// Package inventory deterministically classifies repository source through the
// capability-based boundary without mutating repository or state.
package inventory

import (
	"bytes"
	"errors"
	"os"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/model"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

type Mode string

const (
	ModeBuild    Mode = "build"
	ModeEstimate Mode = "estimate"
)

const binaryPrefixBytes int64 = 8192
const ignorePrefixBytes int64 = 16384

type Path struct {
	RelativePath string
	Language     string
	Size         int64
	SHA256       string
}

type Exclusion struct {
	RelativePath string
	Reason       string
}

type Result struct {
	Paths               []Path
	Exclusions          []Exclusion
	Coverage            model.Coverage
	EligibleSourceBytes uint64
	Partial             bool
	Warnings            []string
}

// Collect inventories source with the frozen production ceilings.
func Collect(roots boundary.Roots, mode Mode) (Result, error) {
	return collect(roots, mode, policy.ProductionLimits())
}

func collect(roots boundary.Roots, mode Mode, limits policy.Limits) (Result, error) {
	if mode != ModeBuild && mode != ModeEstimate {
		return Result{}, errors.New("unsupported inventory mode")
	}
	result := Result{Coverage: model.Coverage{ExclusionReasonCounts: map[string]int{}}}
	if mode == ModeEstimate {
		result.Partial = true
		result.Warnings = append(result.Warnings, "coverage-estimated-not-parsed")
	}
	var ignores []ignoreRule
	err := roots.WalkRepository(func(entry boundary.RepositoryEntry) error {
		// Root ignore policy is deliberately the only parsed ignore file. Nested
		// rules require directory-scoped push/pop semantics and are not silently
		// treated as repository-wide rules.
		if entry.RelativePath == ".gitignore" && entry.Mode.IsRegular() {
			prefix, prefixErr := roots.ReadRepositoryPrefix(entry.RelativePath, ignorePrefixBytes)
			if prefixErr != nil {
				result.Partial = true
				addWarning(&result, "gitignore-unreadable")
			} else {
				ignores = append(ignores, parseIgnoreRules(prefix.Bytes)...)
				if prefix.Size > int64(len(prefix.Bytes)) {
					result.Partial = true
					addWarning(&result, "gitignore-prefix-truncated")
				}
			}
		}
		reason, prune := classifyMetadata(entry, ignores)
		if reason != "" {
			addExclusion(&result, entry.RelativePath, reason)
			if prune {
				return boundary.ErrSkipRepositoryDirectory
			}
			return nil
		}
		if !entry.Mode.IsRegular() {
			return nil
		}
		language := languageForPath(entry.RelativePath)
		if language == "" {
			addExclusion(&result, entry.RelativePath, ExcludedUnsupported)
			return nil
		}
		maximum := int64(limits.MaximumSourceFileBytes)
		if language == "markdown" {
			maximum = int64(limits.MaximumMarkdownFileBytes)
		}
		if entry.Size < 0 || entry.Size > maximum {
			addExclusion(&result, entry.RelativePath, ExcludedOversized)
			return nil
		}
		prefix, prefixErr := roots.ReadRepositoryPrefix(entry.RelativePath, binaryPrefixBytes)
		if prefixErr != nil || prefix.Size != entry.Size {
			addExclusion(&result, entry.RelativePath, ExcludedUnsafe)
			return nil
		}
		if binary(prefix.Bytes) {
			addExclusion(&result, entry.RelativePath, ExcludedBinary)
			return nil
		}
		if len(result.Paths) >= limits.MaximumEligiblePaths {
			addExclusion(&result, entry.RelativePath, ExcludedLimit)
			result.Partial = true
			addWarning(&result, "inventory-path-limit")
			return nil
		}
		if uint64(entry.Size) > limits.MaximumEligibleSourceBytes-result.EligibleSourceBytes {
			addExclusion(&result, entry.RelativePath, ExcludedLimit)
			result.Partial = true
			addWarning(&result, "inventory-byte-limit")
			return nil
		}
		candidate := Path{RelativePath: entry.RelativePath, Language: language, Size: entry.Size}
		if mode == ModeBuild {
			file, fileErr := roots.OpenRepositoryFile(entry.RelativePath, maximum)
			if fileErr != nil || file.Size != entry.Size {
				addExclusion(&result, entry.RelativePath, ExcludedUnsafe)
				return nil
			}
			if binary(file.Bytes) {
				addExclusion(&result, entry.RelativePath, ExcludedBinary)
				return nil
			}
			candidate.SHA256 = file.SHA256
		}
		result.Paths = append(result.Paths, candidate)
		result.EligibleSourceBytes += uint64(entry.Size)
		return nil
	})
	if err != nil {
		return Result{}, err
	}
	sort.Slice(result.Paths, func(i, j int) bool { return result.Paths[i].RelativePath < result.Paths[j].RelativePath })
	sort.Slice(result.Exclusions, func(i, j int) bool {
		if result.Exclusions[i].RelativePath == result.Exclusions[j].RelativePath {
			return result.Exclusions[i].Reason < result.Exclusions[j].Reason
		}
		return result.Exclusions[i].RelativePath < result.Exclusions[j].RelativePath
	})
	sort.Strings(result.Warnings)
	result.Coverage.IndexedPathCount = len(result.Paths)
	result.Coverage.ExcludedPathCount = len(result.Exclusions)
	result.Coverage.UnsupportedLanguageCount = result.Coverage.ExclusionReasonCounts[ExcludedUnsupported]
	result.Coverage.ParseFailureCount = 0
	denominator := result.Coverage.IndexedPathCount + result.Coverage.ExcludedPathCount
	if denominator > 0 {
		result.Coverage.PathCoverage = float64(result.Coverage.IndexedPathCount) / float64(denominator)
		result.Coverage.LanguageCoverage = result.Coverage.PathCoverage
	}
	return result, nil
}

func classifyMetadata(entry boundary.RepositoryEntry, ignores []ignoreRule) (reason string, prune bool) {
	if entry.Mode&os.ModeSymlink != 0 || (!entry.Mode.IsDir() && !entry.Mode.IsRegular()) {
		return ExcludedUnsafe, false
	}
	if reason := excludedDirectory(entry.RelativePath); reason != "" {
		return reason, entry.Mode.IsDir()
	}
	if entry.Mode.IsDir() {
		if ignoredBy(ignores, entry.RelativePath, true) {
			return ExcludedIgnored, true
		}
		return "", false
	}
	if !entry.Mode.IsRegular() {
		return ExcludedUnsafe, false
	}
	if ignoredBy(ignores, entry.RelativePath, false) {
		return ExcludedIgnored, false
	}
	if strings.HasSuffix(strings.ToLower(entry.RelativePath), ".generated.go") || strings.HasSuffix(strings.ToLower(entry.RelativePath), ".gen.go") || strings.HasSuffix(strings.ToLower(entry.RelativePath), ".pb.go") {
		return ExcludedGenerated, false
	}
	return "", false
}

func binary(contents []byte) bool {
	return bytes.IndexByte(contents, 0) >= 0 || !utf8.Valid(contents)
}

func addExclusion(result *Result, relative, reason string) {
	result.Exclusions = append(result.Exclusions, Exclusion{RelativePath: relative, Reason: reason})
	result.Coverage.ExclusionReasonCounts[reason]++
}

func addWarning(result *Result, warning string) {
	for _, existing := range result.Warnings {
		if existing == warning {
			return
		}
	}
	result.Warnings = append(result.Warnings, warning)
}
