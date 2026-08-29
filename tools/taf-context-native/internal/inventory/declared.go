package inventory

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"path"
	"strconv"
	"strings"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/boundary"
)

// DeclaredClassification is the bounded metadata decision for one path named
// by a Level 0 change document.  It deliberately reads only the Git metadata
// and ignore controls that can affect that path; it never inventories the
// repository or opens an undeclared source body.
type DeclaredClassification struct {
	Language      string
	ExclusionPath string
	Exclusion     string
	// ControlIdentity binds the exact bounded Git-index and ignore controls
	// consulted for this declared path. It is an opaque witness used by update's
	// final publication barrier, never a public result field.
	ControlIdentity  string
	AncestorIdentity boundary.FileIdentity
}

func (classification DeclaredClassification) Same(other DeclaredClassification) bool {
	if classification.Language != other.Language || classification.ExclusionPath != other.ExclusionPath || classification.Exclusion != other.Exclusion || classification.ControlIdentity != other.ControlIdentity {
		return false
	}
	if classification.AncestorIdentity.Valid() || other.AncestorIdentity.Valid() {
		return classification.AncestorIdentity.Same(other.AncestorIdentity)
	}
	return true
}

// ClassifyDeclared applies the same deterministic metadata policy as Collect
// to one declared path. Directory exclusions are reported at their canonical
// directory path, matching build-mode inventory's compact catalog.
func ClassifyDeclared(roots *boundary.Roots, relative string) (DeclaredClassification, error) {
	components := strings.Split(relative, "/")
	for index := range components[:len(components)-1] {
		candidate := strings.Join(components[:index+1], "/")
		if reason := excludedDirectory(candidate); reason != "" {
			identity, identityErr := roots.RepositoryDirectoryIdentity(candidate)
			if errors.Is(identityErr, boundary.ErrRepositoryPathNotFound) {
				continue
			}
			if identityErr != nil {
				return DeclaredClassification{}, identityErr
			}
			return DeclaredClassification{ExclusionPath: candidate, Exclusion: reason, ControlIdentity: declaredControlIdentity(relative, nil, gitIndex{}), AncestorIdentity: identity}, nil
		}
	}
	for _, suffix := range generatedSuffixPolicy {
		if strings.HasSuffix(strings.ToLower(relative), suffix) {
			return DeclaredClassification{ExclusionPath: relative, Exclusion: ExcludedGenerated, ControlIdentity: declaredControlIdentity(relative, nil, gitIndex{})}, nil
		}
	}
	language := languageForPath(relative)
	if language == "" {
		return DeclaredClassification{ExclusionPath: relative, Exclusion: ExcludedUnsupported, ControlIdentity: declaredControlIdentity(relative, nil, gitIndex{})}, nil
	}
	// Build continues with an empty tracked set when bounded Git-index metadata
	// is unavailable, marking the full inventory partial. Update must make the
	// same per-path decision rather than inventing a stricter second policy.
	tracked, trackedErr := trackedRepositoryPaths(*roots)
	rules, err := declaredIgnoreRules(roots, path.Dir(relative))
	if err != nil {
		return DeclaredClassification{}, err
	}
	controlBytes, err := declaredControlFingerprint(roots, relative)
	if err != nil {
		return DeclaredClassification{}, err
	}
	control := declaredControlIdentity(relative+"\x00"+trackedErr+"\x00"+controlBytes, rules, tracked)
	if !tracked.isTracked(relative) {
		ignored, limited := ignoredBy(rules, relative, false, newIgnoreMatchBudget(maximumIgnoreRuleEvaluations, maximumIgnoreMatchWork))
		if limited {
			return DeclaredClassification{}, errors.New("declared gitignore match limit")
		}
		if ignored {
			return DeclaredClassification{ExclusionPath: relative, Exclusion: ExcludedIgnored, ControlIdentity: control}, nil
		}
	}
	return DeclaredClassification{Language: language, ControlIdentity: control}, nil
}

func declaredControlIdentity(relative string, rules []ignoreRule, tracked gitIndex) string {
	var canonical strings.Builder
	canonical.WriteString("declared-v1\x00")
	canonical.WriteString(relative)
	for _, rule := range rules {
		canonical.WriteString("\x00r\x00")
		canonical.WriteString(rule.base)
		canonical.WriteString("\x00")
		canonical.WriteString(rule.pattern)
		if rule.negated {
			canonical.WriteByte('n')
		}
		if rule.directory {
			canonical.WriteByte('d')
		}
		if rule.anchored {
			canonical.WriteByte('a')
		}
		if rule.slash {
			canonical.WriteByte('s')
		}
	}
	for index, trackedPath := range tracked.paths {
		canonical.WriteString("\x00t\x00")
		canonical.WriteString(trackedPath)
		canonical.WriteString("\x00")
		if index < len(tracked.modes) {
			canonical.WriteString(strconv.FormatUint(uint64(tracked.modes[index]), 10))
		}
	}
	digest := sha256.Sum256([]byte(canonical.String()))
	return "sha256:" + hex.EncodeToString(digest[:])
}

// declaredControlFingerprint witnesses the raw bounded controls, not merely
// their parsed semantic rules: a comment-only .gitignore replacement must
// still invalidate an in-flight update because the control was consulted.
func declaredControlFingerprint(roots *boundary.Roots, relative string) (string, error) {
	hash := sha256.New()
	write := func(name string, file boundary.StableFile, err error) error {
		hash.Write([]byte(name))
		if errors.Is(err, boundary.ErrRepositoryPathNotFound) || errors.Is(err, boundary.ErrGitMetadataNotFound) {
			hash.Write([]byte("\x00missing\x00"))
			return nil
		}
		if err != nil {
			return err
		}
		hash.Write([]byte("\x00" + strconv.FormatInt(file.Size, 10) + "\x00"))
		hash.Write(file.Bytes)
		return nil
	}
	index, indexErr := roots.OpenGitMetadataFile("index", maximumGitIndexBytes)
	if err := write("git-index", index, indexErr); err != nil {
		return "", err
	}
	common, commonErr := roots.OpenGitCommonMetadataFile("info/exclude", ignorePrefixBytes)
	if err := write("git-info-exclude", common, commonErr); err != nil {
		return "", err
	}
	directory := path.Dir(relative)
	parts := []string{}
	if directory != "." {
		parts = strings.Split(directory, "/")
	}
	for index := 0; index <= len(parts); index++ {
		base := strings.Join(parts[:index], "/")
		name := ".gitignore"
		if base != "" {
			name = base + "/" + name
		}
		file, readErr := roots.ReadRepositoryPrefix(name, ignorePrefixBytes)
		if err := write(name, boundary.StableFile{Bytes: file.Bytes, Size: file.Size}, readErr); err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func declaredIgnoreRules(roots *boundary.Roots, directory string) ([]ignoreRule, error) {
	var rules []ignoreRule
	patternBytes := 0
	appendRules := func(base string, bytes []byte) error {
		parsed, used, limited := parseIgnoreRules(base, bytes, maximumIgnoreRules-len(rules), maximumIgnorePatternBytes-patternBytes)
		if limited {
			return errors.New("declared gitignore rule limit")
		}
		rules = append(rules, parsed...)
		patternBytes += used
		return nil
	}
	if common, err := roots.OpenGitCommonMetadataFile("info/exclude", ignorePrefixBytes); err == nil {
		if common.Size > int64(len(common.Bytes)) {
			return nil, errors.New("declared git info exclude prefix truncated")
		}
		if err := appendRules("", common.Bytes); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, boundary.ErrGitMetadataNotFound) {
		return nil, err
	}
	parts := []string{}
	if directory != "." {
		parts = strings.Split(directory, "/")
	}
	for index := 0; index <= len(parts); index++ {
		base := strings.Join(parts[:index], "/")
		name := ".gitignore"
		if base != "" {
			name = base + "/" + name
		}
		file, err := roots.ReadRepositoryPrefix(name, ignorePrefixBytes)
		if errors.Is(err, boundary.ErrRepositoryPathNotFound) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if file.Size > int64(len(file.Bytes)) {
			return nil, errors.New("declared gitignore prefix truncated")
		}
		if err := appendRules(base, file.Bytes); err != nil {
			return nil, err
		}
	}
	return rules, nil
}
