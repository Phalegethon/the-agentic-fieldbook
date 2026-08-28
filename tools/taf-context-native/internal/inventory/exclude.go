package inventory

import (
	"path"
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
	pattern   string
	negated   bool
	directory bool
}

func parseIgnoreRules(contents []byte) []ignoreRule {
	var rules []ignoreRule
	for _, line := range strings.Split(string(contents), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		rule := ignoreRule{negated: strings.HasPrefix(line, "!")}
		if rule.negated {
			line = strings.TrimPrefix(line, "!")
		}
		rule.directory = strings.HasSuffix(line, "/")
		rule.pattern = strings.Trim(strings.TrimSuffix(line, "/"), "/")
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
	pattern := rule.pattern
	if rule.directory {
		for _, component := range strings.Split(relative, "/") {
			if globMatches(pattern, component) {
				return true
			}
		}
		return false
	}
	if strings.Contains(pattern, "/") {
		return globMatches(pattern, relative)
	}
	return globMatches(pattern, path.Base(relative))
}

func globMatches(pattern, value string) bool {
	matched, err := path.Match(pattern, value)
	return err == nil && matched
}

func languageForPath(relative string) string {
	extension := strings.ToLower(path.Ext(relative))
	switch extension {
	case ".go":
		return "go"
	case ".py":
		return "python"
	case ".js", ".mjs", ".cjs", ".jsx":
		return "javascript"
	case ".ts", ".tsx":
		return "typescript"
	case ".rs":
		return "rust"
	case ".md", ".mdx":
		return "markdown"
	default:
		return ""
	}
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
