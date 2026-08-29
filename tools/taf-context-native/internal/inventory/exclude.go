package inventory

import (
	"crypto/sha256"
	endian "encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/policy"
)

const (
	ExcludedGit         = "git-metadata"
	ExcludedGenerated   = "generated"
	ExcludedVendored    = "vendored"
	ExcludedIgnored     = "ignored"
	ExcludedBinary      = "binary"
	ExcludedOversized   = "oversized"
	ExcludedUnsupported = "unsupported-language"
	ExcludedUnsafe      = "unsafe-path"
	ExcludedLimit       = "policy-limit"
)

type ignoreRule struct {
	base      string
	pattern   string
	negated   bool
	directory bool
	anchored  bool
	slash     bool
	matcher   *regexp.Regexp
}

const (
	maximumIgnoreRules           = 256
	maximumIgnorePatternBytes    = 64 << 10
	maximumIgnorePatternSize     = 1024
	maximumIgnoreRuleEvaluations = 1_000_000
	maximumIgnoreMatchWork       = 64 << 20
)

type ignoreMatchBudget struct {
	remainingEvaluations int
	remainingWork        int
	observe              func(pattern, candidate string)
}

func newIgnoreMatchBudget(evaluations, work int) *ignoreMatchBudget {
	return &ignoreMatchBudget{remainingEvaluations: evaluations, remainingWork: work}
}

func (budget *ignoreMatchBudget) consume(rule ignoreRule, candidate string) bool {
	if budget == nil {
		return true
	}
	patternWork := len(rule.pattern) + 1
	candidateWork := len(candidate) + 1
	if budget.remainingEvaluations <= 0 || patternWork > budget.remainingWork/candidateWork {
		return false
	}
	cost := patternWork * candidateWork
	budget.remainingEvaluations--
	budget.remainingWork -= cost
	return true
}

// LanguageMetadata is immutable extension metadata shared by inventory and
// later extractor registration. ExtensionRegistry returns a defensive copy.
type LanguageMetadata struct {
	Language   string
	Extensions []string
	Markdown   bool
}

var extensionRegistry = []LanguageMetadata{
	{Language: "go", Extensions: []string{".go"}},
	{Language: "python", Extensions: []string{".py"}},
	{Language: "javascript", Extensions: []string{".js", ".mjs", ".cjs", ".jsx"}},
	{Language: "typescript", Extensions: []string{".ts", ".tsx"}},
	{Language: "rust", Extensions: []string{".rs"}},
	{Language: "markdown", Extensions: []string{".md", ".mdx"}, Markdown: true},
	{Language: "json", Extensions: []string{".json"}},
	{Language: "toml", Extensions: []string{".toml"}},
}

var excludedDirectoryPolicy = []struct{ component, reason string }{
	{".git", ExcludedGit}, {"vendor", ExcludedVendored}, {"vendors", ExcludedVendored}, {"node_modules", ExcludedVendored}, {"third_party", ExcludedVendored},
	{"generated", ExcludedGenerated}, {"dist", ExcludedGenerated}, {"build", ExcludedGenerated}, {"coverage", ExcludedGenerated}, {".next", ExcludedGenerated}, {"target", ExcludedGenerated},
}

var generatedSuffixPolicy = []string{".generated.go", ".gen.go", ".pb.go"}

var policyAlgorithmTokens = []string{"git-index-v4-bounded-v1", "descriptor-walk-snapshot-v2", "gitignore-glob-v2", "binary-prefix-utf8-v2", "unsafe-symlink-classification-v1"}

func ExtensionRegistry() []LanguageMetadata {
	copyRegistry := make([]LanguageMetadata, len(extensionRegistry))
	for index, metadata := range extensionRegistry {
		copyRegistry[index] = LanguageMetadata{Language: metadata.Language, Markdown: metadata.Markdown, Extensions: append([]string(nil), metadata.Extensions...)}
	}
	return copyRegistry
}

// PolicyIdentities binds the installed immutable inclusion and exclusion
// surfaces that Collect actually consults.
func PolicyIdentities() (string, string) {
	return policyIdentities(policy.ProductionLimits())
}

func policyIdentities(limits policy.Limits) (string, string) {
	limitBytes, _ := json.Marshal(limits)
	inclusion := []string{"taf-level1-inclusion-v1"}
	for _, metadata := range extensionRegistry {
		inclusion = append(inclusion, metadata.Language)
		inclusion = append(inclusion, metadata.Extensions...)
		if metadata.Markdown {
			inclusion = append(inclusion, "markdown-size-ceiling")
		}
	}
	inclusion = append(inclusion, string(limitBytes))
	exclusion := []string{"taf-level1-exclusion-v1", string(limitBytes), boundary.PolicyDescriptor(), fmt.Sprintf("ignore=%d,%d,%d,%d,%d prefix=%d,%d git=%d,%d,%d,%d,%d,%d,%d,%d", maximumIgnoreRules, maximumIgnorePatternBytes, maximumIgnorePatternSize, maximumIgnoreRuleEvaluations, maximumIgnoreMatchWork, binaryPrefixBytes, ignorePrefixBytes, maximumGitIndexBytes, maximumGitIndexEntries, maximumGitIndexPathBytes, maximumGitIndexDecodedPathBytes, maximumGitIndexPathComponents, maximumGitIndexDecodedComponents, maximumGitIndexDerivedMetadataBytes, gitIndexDerivedBytesPerPath), ExcludedGit, ExcludedGenerated, ExcludedVendored, ExcludedIgnored, ExcludedBinary, ExcludedOversized, ExcludedUnsupported, ExcludedUnsafe, ExcludedLimit}
	for _, rule := range excludedDirectoryPolicy {
		exclusion = append(exclusion, rule.component, rule.reason)
	}
	exclusion = append(exclusion, generatedSuffixPolicy...)
	exclusion = append(exclusion, policyAlgorithmTokens...)
	return policyDigest(inclusion), policyDigest(exclusion)
}

func policyDigest(parts []string) string {
	hash := sha256.New()
	var size [8]byte
	for _, part := range parts {
		endian.BigEndian.PutUint64(size[:], uint64(len(part)))
		_, _ = hash.Write(size[:])
		_, _ = hash.Write([]byte(part))
	}
	return "sha256:" + hex.EncodeToString(hash.Sum(nil))
}

func parseIgnoreRules(base string, contents []byte, availableRules, availablePatternBytes int) ([]ignoreRule, int, bool) {
	var rules []ignoreRule
	patternBytes := 0
	for _, line := range strings.Split(string(contents), "\n") {
		line = strings.TrimSuffix(line, "\r")
		line = trimUnescapedTrailingSpaces(line)
		if line == "" {
			continue
		}
		escapedLeading := strings.HasPrefix(line, `\!`) || strings.HasPrefix(line, `\#`)
		if !escapedLeading && strings.HasPrefix(line, "#") {
			continue
		}
		rule := ignoreRule{base: base, negated: !escapedLeading && strings.HasPrefix(line, "!")}
		if escapedLeading {
			line = strings.TrimPrefix(line, `\`)
		} else if rule.negated {
			line = strings.TrimPrefix(line, "!")
		}
		if line == "" {
			continue
		}
		rule.directory = strings.HasSuffix(line, "/") && !escapedByte(line, len(line)-1)
		rule.anchored = strings.HasPrefix(line, "/")
		if rule.anchored {
			line = line[1:]
		}
		if rule.directory && strings.HasSuffix(line, "/") {
			line = line[:len(line)-1]
		}
		rule.pattern = line
		rule.slash = strings.Contains(line, "/")
		if rule.pattern == "" {
			continue
		}
		if len(rules) >= availableRules || len(rule.pattern) > maximumIgnorePatternSize || patternBytes+len(rule.pattern) > availablePatternBytes {
			return rules, patternBytes, true
		}
		matcher, ok := compileGitGlob(rule.pattern)
		if !ok {
			continue
		}
		rule.matcher = matcher
		rules = append(rules, rule)
		patternBytes += len(rule.pattern)
	}
	return rules, patternBytes, false
}

func ignoredBy(rules []ignoreRule, relative string, directory bool, budget *ignoreMatchBudget) (bool, bool) {
	lastSlash := strings.LastIndexByte(relative, '/')
	for index := 0; index <= lastSlash; index++ {
		if relative[index] != '/' {
			continue
		}
		ignored, limited := ignoredDirectlyBy(rules, relative[:index], true, budget)
		if limited || ignored {
			return ignored, limited
		}
	}
	return ignoredDirectlyBy(rules, relative, directory, budget)
}

func ignoredDirectlyBy(rules []ignoreRule, relative string, directory bool, budget *ignoreMatchBudget) (bool, bool) {
	ignored := false
	for _, rule := range rules {
		matched, limited := ignoreRuleMatches(rule, relative, directory, budget)
		if limited {
			return false, true
		}
		if matched {
			ignored = !rule.negated
		}
	}
	return ignored, false
}

func ignoreRuleMatches(rule ignoreRule, relative string, directory bool, budget *ignoreMatchBudget) (bool, bool) {
	if rule.base != "" {
		if len(relative) <= len(rule.base) || relative[:len(rule.base)] != rule.base || relative[len(rule.base)] != '/' {
			return false, false
		}
		relative = relative[len(rule.base)+1:]
	}
	if rule.directory {
		if !directory {
			return false, false
		}
		if rule.slash {
			// ignoredBy already supplies every ancestor as a directory candidate.
			// Rescanning those ancestors here duplicates regex work by depth.
			return matchIgnoreCandidate(rule, relative, budget)
		}
		if rule.anchored {
			return matchIgnoreCandidate(rule, relative, budget)
		}
		return matchIgnoreCandidate(rule, ignoreCandidateBase(relative), budget)
	}
	if rule.slash || rule.anchored {
		return matchIgnoreCandidate(rule, relative, budget)
	}
	return matchIgnoreCandidate(rule, ignoreCandidateBase(relative), budget)
}

func ignoreCandidateBase(relative string) string {
	if slash := strings.LastIndexByte(relative, '/'); slash >= 0 {
		return relative[slash+1:]
	}
	return relative
}

func matchIgnoreCandidate(rule ignoreRule, candidate string, budget *ignoreMatchBudget) (bool, bool) {
	if !budget.consume(rule, candidate) {
		return false, true
	}
	if budget != nil && budget.observe != nil {
		budget.observe(rule.pattern, candidate)
	}
	return rule.matcher.MatchString(candidate), false
}

func compileGitGlob(pattern string) (*regexp.Regexp, bool) {
	if !utf8.ValidString(pattern) {
		return nil, false
	}
	runes := []rune(pattern)
	var expression strings.Builder
	expression.WriteString("^")
	for index := 0; index < len(runes); index++ {
		switch runes[index] {
		case '\\':
			if index+1 < len(runes) {
				index++
				expression.WriteString(regexp.QuoteMeta(string(runes[index])))
			} else {
				expression.WriteString(`\\`)
			}
		case '*':
			end := index + 1
			for end < len(runes) && runes[end] == '*' {
				end++
			}
			runLength := end - index
			leading := runLength == 2 && index == 0 && end < len(runes) && runes[end] == '/'
			trailing := runLength == 2 && index > 0 && runes[index-1] == '/' && end == len(runes)
			betweenSlashes := runLength == 2 && index > 0 && runes[index-1] == '/' && end < len(runes) && runes[end] == '/'
			switch {
			case leading, betweenSlashes:
				expression.WriteString("(?:[^/]+/)*")
				end++ // The special form owns its following slash.
			case trailing:
				expression.WriteString(".*")
			default:
				// Consecutive stars outside Git's three documented ** forms
				// are equivalent to one ordinary star and cannot match '/'.
				expression.WriteString("[^/]*")
			}
			index = end - 1
		case '?':
			expression.WriteString("[^/]")
		case '[':
			class, end, ok := compileGitBracket(runes, index)
			if !ok {
				return nil, false
			}
			expression.WriteString(class)
			index = end
		default:
			expression.WriteString(regexp.QuoteMeta(string(runes[index])))
		}
	}
	expression.WriteString("$")
	compiled, err := regexp.Compile(expression.String())
	return compiled, err == nil
}

func compileGitBracket(pattern []rune, start int) (string, int, bool) {
	end := start + 1
	escaped := false
	for ; end < len(pattern); end++ {
		if !escaped && pattern[end] == ']' && end > start+1 {
			break
		}
		if !escaped && pattern[end] == '\\' {
			escaped = true
		} else {
			escaped = false
		}
	}
	if end == len(pattern) {
		return "", 0, false
	}
	contents := pattern[start+1 : end]
	var expression strings.Builder
	expression.WriteByte('[')
	index := 0
	if contents[0] == '!' || contents[0] == '^' {
		expression.WriteByte('^')
		index++
	}
	if index == len(contents) {
		return "", 0, false
	}
	for ; index < len(contents); index++ {
		character := contents[index]
		if character == '\\' && index+1 < len(contents) {
			index++
			character = contents[index]
		}
		switch character {
		case '\\', '[', ']', '^':
			expression.WriteByte('\\')
			expression.WriteRune(character)
		case '-':
			if index == 0 || index == len(contents)-1 {
				expression.WriteString(`\-`)
			} else {
				expression.WriteRune('-')
			}
		default:
			expression.WriteRune(character)
		}
	}
	expression.WriteByte(']')
	return expression.String(), end, true
}

func trimUnescapedTrailingSpaces(line string) string {
	for len(line) > 0 && line[len(line)-1] == ' ' && !escapedByte(line, len(line)-1) {
		line = line[:len(line)-1]
	}
	return line
}

func escapedByte(value string, index int) bool {
	backslashes := 0
	for index--; index >= 0 && value[index] == '\\'; index-- {
		backslashes++
	}
	return backslashes%2 == 1
}

func languageForPath(relative string) string {
	extension := strings.ToLower(path.Ext(relative))
	for _, metadata := range extensionRegistry {
		for _, candidate := range metadata.Extensions {
			if extension == candidate {
				return metadata.Language
			}
		}
	}
	return ""
}

func markdownLanguage(language string) bool {
	for _, metadata := range extensionRegistry {
		if metadata.Language == language {
			return metadata.Markdown
		}
	}
	return false
}

func excludedDirectory(relative string) string {
	for _, component := range strings.Split(relative, "/") {
		component = strings.ToLower(component)
		for _, rule := range excludedDirectoryPolicy {
			if component == rule.component {
				return rule.reason
			}
		}
	}
	return ""
}
