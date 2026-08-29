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
	UnknownRemainder    bool
	DirectoryEntries    int
	PrefixBytes         uint64
	FullBodyOpens       int
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
	tracked, indexWarning := trackedRepositoryPaths(roots)
	if indexWarning != "" {
		result.Partial = true
		addWarning(&result, indexWarning)
	}
	var ignores []ignoreRule
	regularExclusions, languageSupported, languageUnsupported := 0, 0, 0
	err := roots.WalkRepository(func(entry boundary.RepositoryEntry) error {
		if result.DirectoryEntries >= policy.ProductionLimits().MaximumEligiblePaths {
			result.Partial, result.UnknownRemainder = true, true
			addWarning(&result, "inventory-entry-limit")
			return boundary.ErrStopRepositoryWalk
		}
		result.DirectoryEntries++
		if strings.HasSuffix(entry.RelativePath, ".gitignore") && entry.Mode.IsRegular() {
			prefix, prefixErr := roots.ReadRepositoryPrefix(entry.RelativePath, ignorePrefixBytes)
			if prefixErr != nil {
				result.Partial = true
				addWarning(&result, "gitignore-unreadable")
			} else {
				base := strings.TrimSuffix(strings.TrimSuffix(entry.RelativePath, ".gitignore"), "/")
				ignores = append(ignores, parseIgnoreRules(base, prefix.Bytes)...)
				result.PrefixBytes += uint64(len(prefix.Bytes))
				if prefix.Size > int64(len(prefix.Bytes)) {
					result.Partial = true
					addWarning(&result, "gitignore-prefix-truncated")
				}
			}
		}
		reason, prune := classifyMetadata(entry, ignores, tracked)
		if reason != "" {
			addExclusion(&result, entry.RelativePath, reason)
			if entry.Mode.IsRegular() {
				regularExclusions++
			}
			if prune {
				if reason != ExcludedGit {
					result.Partial, result.UnknownRemainder = true, true
					addWarning(&result, "inventory-pruned-subtree")
				}
				return boundary.ErrSkipRepositoryDirectory
			}
			return nil
		}
		if !entry.Mode.IsRegular() {
			return nil
		}
		language := languageForPath(entry.RelativePath)
		if language == "" {
			languageUnsupported++
			addExclusion(&result, entry.RelativePath, ExcludedUnsupported)
			regularExclusions++
			return nil
		}
		languageSupported++
		maximum := int64(limits.MaximumSourceFileBytes)
		if markdownLanguage(language) {
			maximum = int64(limits.MaximumMarkdownFileBytes)
		}
		if entry.Size < 0 || entry.Size > maximum {
			addExclusion(&result, entry.RelativePath, ExcludedOversized)
			regularExclusions++
			return nil
		}
		if len(result.Paths) >= limits.MaximumEligiblePaths || uint64(entry.Size) > limits.MaximumEligibleSourceBytes-result.EligibleSourceBytes {
			result.Partial, result.UnknownRemainder = true, true
			if len(result.Paths) >= limits.MaximumEligiblePaths {
				addWarning(&result, "inventory-path-limit")
			} else {
				addWarning(&result, "inventory-byte-limit")
			}
			return boundary.ErrStopRepositoryWalk
		}
		prefix, prefixErr := roots.ReadRepositoryPrefix(entry.RelativePath, binaryPrefixBytes)
		if prefixErr == nil {
			result.PrefixBytes += uint64(len(prefix.Bytes))
		}
		if prefixErr != nil || prefix.Size != entry.Size {
			addExclusion(&result, entry.RelativePath, ExcludedUnsafe)
			regularExclusions++
			return nil
		}
		if binary(prefix.Bytes, prefix.Size > int64(len(prefix.Bytes))) {
			addExclusion(&result, entry.RelativePath, ExcludedBinary)
			regularExclusions++
			return nil
		}
		candidate := Path{RelativePath: entry.RelativePath, Language: language, Size: entry.Size}
		if mode == ModeBuild {
			result.FullBodyOpens++
			file, fileErr := roots.OpenRepositoryFile(entry.RelativePath, maximum)
			if fileErr != nil || file.Size != entry.Size {
				addExclusion(&result, entry.RelativePath, ExcludedUnsafe)
				regularExclusions++
				return nil
			}
			if binary(file.Bytes, false) {
				addExclusion(&result, entry.RelativePath, ExcludedBinary)
				regularExclusions++
				return nil
			}
			candidate.SHA256 = file.SHA256
		}
		result.Paths = append(result.Paths, candidate)
		result.EligibleSourceBytes += uint64(entry.Size)
		return nil
	})
	if errors.Is(err, boundary.ErrRepositoryEnumerationLimit) {
		result.Partial, result.UnknownRemainder = true, true
		addWarning(&result, "inventory-directory-batch-limit")
	} else if err != nil && !errors.Is(err, boundary.ErrStopRepositoryWalk) {
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
	result.Coverage.ExcludedPathCount = regularExclusions
	result.Coverage.UnsupportedLanguageCount = result.Coverage.ExclusionReasonCounts[ExcludedUnsupported]
	result.Coverage.ParseFailureCount = 0
	denominator := result.Coverage.IndexedPathCount + regularExclusions
	if denominator > 0 && !result.UnknownRemainder {
		result.Coverage.PathCoverage = float64(result.Coverage.IndexedPathCount) / float64(denominator)
	}
	languageDenominator := languageSupported + languageUnsupported
	if languageDenominator > 0 && !result.UnknownRemainder {
		result.Coverage.LanguageCoverage = float64(languageSupported) / float64(languageDenominator)
	}
	return result, nil
}

func classifyMetadata(entry boundary.RepositoryEntry, ignores []ignoreRule, tracked map[string]struct{}) (reason string, prune bool) {
	if entry.GitMetadata {
		return ExcludedGit, entry.Mode.IsDir()
	}
	if entry.Mode&os.ModeSymlink != 0 || (!entry.Mode.IsDir() && !entry.Mode.IsRegular()) {
		return ExcludedUnsafe, false
	}
	if reason := excludedDirectory(entry.RelativePath); reason != "" {
		return reason, entry.Mode.IsDir()
	}
	if entry.Mode.IsDir() {
		return "", false
	}
	if !entry.Mode.IsRegular() {
		return ExcludedUnsafe, false
	}
	if _, isTracked := tracked[entry.RelativePath]; !isTracked && ignoredBy(ignores, entry.RelativePath, false) {
		return ExcludedIgnored, false
	}
	if strings.HasSuffix(strings.ToLower(entry.RelativePath), ".generated.go") || strings.HasSuffix(strings.ToLower(entry.RelativePath), ".gen.go") || strings.HasSuffix(strings.ToLower(entry.RelativePath), ".pb.go") {
		return ExcludedGenerated, false
	}
	return "", false
}

func binary(contents []byte, truncated bool) bool {
	if bytes.IndexByte(contents, 0) >= 0 {
		return true
	}
	if utf8.Valid(contents) {
		return false
	}
	if truncated {
		for suffix := 1; suffix <= 3 && suffix < len(contents); suffix++ {
			prefix, tail := contents[:len(contents)-suffix], contents[len(contents)-suffix:]
			if utf8.Valid(prefix) && utf8.RuneStart(tail[0]) && !utf8.FullRune(tail) {
				return false
			}
		}
	}
	return true
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
