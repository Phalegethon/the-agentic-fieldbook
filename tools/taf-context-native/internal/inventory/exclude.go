package inventory

import (
	"path"
	"regexp"
	"strings"
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
	matcher   *regexp.Regexp
}

const (
	maximumIgnoreRules        = 256
	maximumIgnorePatternBytes = 64 << 10
	maximumIgnorePatternSize  = 1024
)

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

func ExtensionRegistry() []LanguageMetadata {
	copyRegistry := make([]LanguageMetadata, len(extensionRegistry))
	for index, metadata := range extensionRegistry {
		copyRegistry[index] = LanguageMetadata{Language: metadata.Language, Markdown: metadata.Markdown, Extensions: append([]string(nil), metadata.Extensions...)}
	}
	return copyRegistry
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
		rule.directory = strings.HasSuffix(line, "/") && !escapedByte(line, len(line)-1)
		rule.anchored = strings.HasPrefix(line, "/")
		if rule.anchored {
			line = line[1:]
		}
		if rule.directory {
			line = line[:len(line)-1]
		}
		rule.pattern = line
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

func ignoredBy(rules []ignoreRule, relative string, directory bool) bool {
	parent := path.Dir(relative)
	if parent != "." {
		components := strings.Split(parent, "/")
		for index := range components {
			ancestor := strings.Join(components[:index+1], "/")
			if ignoredDirectlyBy(rules, ancestor, true) {
				return true
			}
		}
	}
	return ignoredDirectlyBy(rules, relative, directory)
}

func ignoredDirectlyBy(rules []ignoreRule, relative string, directory bool) bool {
	ignored := false
	for _, rule := range rules {
		if ignoreRuleMatches(rule, relative, directory) {
			ignored = !rule.negated
		}
	}
	return ignored
}

func ignoreRuleMatches(rule ignoreRule, relative string, directory bool) bool {
	if rule.base != "" {
		if relative == rule.base {
			return false
		}
		prefix := rule.base + "/"
		if !strings.HasPrefix(relative, prefix) {
			return false
		}
		relative = strings.TrimPrefix(relative, prefix)
	}
	pattern := rule.pattern
	if rule.directory {
		if strings.Contains(pattern, "/") {
			candidate := relative
			if !directory {
				candidate = path.Dir(relative)
			}
			for candidate != "." && candidate != "" {
				if rule.matcher.MatchString(candidate) {
					return true
				}
				candidate = path.Dir(candidate)
			}
			return false
		}
		for _, component := range strings.Split(relative, "/") {
			if rule.matcher.MatchString(component) {
				return true
			}
		}
		return false
	}
	if strings.Contains(pattern, "/") || rule.anchored {
		return rule.matcher.MatchString(relative)
	}
	for _, component := range strings.Split(relative, "/") {
		if rule.matcher.MatchString(component) {
			return true
		}
	}
	return false
}

func compileGitGlob(pattern string) (*regexp.Regexp, bool) {
	var expression strings.Builder
	expression.WriteString("^")
	for index := 0; index < len(pattern); index++ {
		switch pattern[index] {
		case '\\':
			if index+1 < len(pattern) {
				index++
				expression.WriteString(regexp.QuoteMeta(string(pattern[index])))
			} else {
				expression.WriteString(`\\`)
			}
		case '*':
			if index+1 < len(pattern) && pattern[index+1] == '*' {
				index++
				if index+1 < len(pattern) && pattern[index+1] == '/' {
					index++
					expression.WriteString("(?:.*/)?")
				} else {
					expression.WriteString(".*")
				}
			} else {
				expression.WriteString("[^/]*")
			}
		case '?':
			expression.WriteString("[^/]")
		case '[':
			class, end, ok := compileGitBracket(pattern, index)
			if !ok {
				return nil, false
			}
			expression.WriteString(class)
			index = end
		default:
			expression.WriteString(regexp.QuoteMeta(string(pattern[index])))
		}
	}
	expression.WriteString("$")
	compiled, err := regexp.Compile(expression.String())
	return compiled, err == nil
}

func compileGitBracket(pattern string, start int) (string, int, bool) {
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
	for ; index < len(contents); index++ {
		character := contents[index]
		if character == '\\' && index+1 < len(contents) {
			index++
			character = contents[index]
		}
		switch character {
		case '\\', ']', '^':
			expression.WriteByte('\\')
			expression.WriteByte(character)
		case '-':
			if index == 0 || index == len(contents)-1 {
				expression.WriteString(`\-`)
			} else {
				expression.WriteByte('-')
			}
		default:
			expression.WriteByte(character)
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
		switch strings.ToLower(component) {
		case ".git":
			return ExcludedGit
		case "vendor", "vendors", "node_modules", "third_party":
			return ExcludedVendored
		case "generated", "dist", "build", "coverage", ".next", "target":
			return ExcludedGenerated
		}
	}
	return ""
}
