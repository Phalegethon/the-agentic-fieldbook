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

func ExtensionRegistry() []LanguageMetadata {
	copyRegistry := make([]LanguageMetadata, len(extensionRegistry))
	for index, metadata := range extensionRegistry {
		copyRegistry[index] = LanguageMetadata{Language: metadata.Language, Markdown: metadata.Markdown, Extensions: append([]string(nil), metadata.Extensions...)}
	}
	return copyRegistry
}

func parseIgnoreRules(base string, contents []byte) []ignoreRule {
	var rules []ignoreRule
	for _, line := range strings.Split(string(contents), "\n") {
		line = strings.TrimSpace(line)
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
		rule.directory = strings.HasSuffix(line, "/")
		rule.anchored = strings.HasPrefix(line, "/")
		line = strings.TrimPrefix(line, "/")
		rule.pattern = strings.TrimSuffix(line, "/")
		if rule.pattern != "" {
			rules = append(rules, rule)
		}
	}
	return rules
}

func ignoredBy(rules []ignoreRule, relative string, directory bool) bool {
	ignored := false
	for _, rule := range rules {
		if rule.directory && !directory && !strings.Contains(relative, "/") {
			continue
		}
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
				if globMatches(pattern, candidate) {
					return true
				}
				candidate = path.Dir(candidate)
			}
			return false
		}
		for _, component := range strings.Split(relative, "/") {
			if globMatches(pattern, component) {
				return true
			}
		}
		return false
	}
	if strings.Contains(pattern, "/") || rule.anchored {
		return globMatches(pattern, relative)
	}
	for _, component := range strings.Split(relative, "/") {
		if globMatches(pattern, component) {
			return true
		}
	}
	return false
}

func globMatches(pattern, value string) bool {
	var expression strings.Builder
	expression.WriteString("^")
	for index := 0; index < len(pattern); index++ {
		switch pattern[index] {
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
		default:
			expression.WriteString(regexp.QuoteMeta(string(pattern[index])))
		}
	}
	expression.WriteString("$")
	compiled, err := regexp.Compile(expression.String())
	return err == nil && compiled.MatchString(value)
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

func doubleStarMatch(pattern, value string) bool {
	for strings.Contains(pattern, "**/") {
		prefix, suffix, _ := strings.Cut(pattern, "**/")
		if prefix == "" {
			if globMatches(suffix, value) {
				return true
			}
			for index := range value {
				if value[index] == '/' && globMatches(suffix, value[index+1:]) {
					return true
				}
			}
		}
		break
	}
	// Git's ** here is an arbitrary sequence including slashes. A compact
	// backtracking matcher keeps matching bounded by the already bounded path.
	return globRecursive(pattern, value)
}

func globRecursive(pattern, value string) bool {
	if pattern == "" {
		return value == ""
	}
	if strings.HasPrefix(pattern, "**") {
		for index := 0; index <= len(value); index++ {
			if globRecursive(pattern[2:], value[index:]) {
				return true
			}
		}
		return false
	}
	if pattern[0] == '*' {
		for index := 0; index <= len(value) && (index == 0 || value[index-1] != '/'); index++ {
			if globRecursive(pattern[1:], value[index:]) {
				return true
			}
		}
		return false
	}
	if len(value) == 0 {
		return false
	}
	if pattern[0] == '?' {
		return value[0] != '/' && globRecursive(pattern[1:], value[1:])
	}
	return pattern[0] == value[0] && globRecursive(pattern[1:], value[1:])
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
