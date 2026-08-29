// Package boundary establishes the filesystem trust boundary for the native engine.
package boundary

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/Phalegethon/the-agentic-fieldbook/tools/taf-context-native/internal/wire"
)

var (
	ErrRootOverlap         = errors.New("repository and state roots overlap")
	ErrUnsafeRoot          = errors.New("unsafe repository or state root")
	ErrUnsafePath          = errors.New("unsafe repository path")
	ErrUnstableFile        = errors.New("repository file changed while being read")
	ErrFileTooLarge        = errors.New("repository file exceeds maximum size")
	ErrStateUnavailable    = errors.New("state root has not been created")
	ErrGitMetadataNotFound = errors.New("Git metadata file not found")
	ErrRepositoryChanged   = errors.New("repository changed during enumeration")
	// ErrSkipRepositoryDirectory lets a metadata consumer prune one safe
	// directory without making the walker follow it.
	ErrSkipRepositoryDirectory    = errors.New("skip repository directory")
	ErrStopRepositoryWalk         = errors.New("stop repository walk")
	ErrRepositoryEnumerationLimit = errors.New("repository directory enumeration limit")
)

const maximumGitMetadataBytes = 4096

// Roots owns the directory handles captured during validation. It must be
// closed once the request finishes; copies share those handles.
type Roots struct {
	Repository         string
	State              string
	GitDirectory       string
	GitCommonDirectory string
	GitDir             string
	GitCommonDir       string

	repositoryRoot   *os.Root
	repositoryInfo   os.FileInfo
	stateRoot        *os.Root
	stateParent      *os.Root
	stateParts       []string
	gitDirectoryRoot *os.Root
	gitDirectoryInfo os.FileInfo
	gitCommonRoot    *os.Root
	gitCommonInfo    os.FileInfo
	gitMetadataInfo  os.FileInfo
	ioObservation    *ioObservationState
}

// stateCreateHook is a deterministic test seam for an adversarial creation
// window. Production leaves it nil.
var stateCreateHook func(component string)

var captureBeforeOpenHook func(path string)
var metadataOpenHook func(name string)
var metadataTargetCaptureHook func(name string)
var gitDiscoveryBeforeOpenHook func()
var stateEnsureBeforeOpenHook func(component string)
var identityEqualHook func(first, second os.FileInfo) bool

func sameIdentity(first, second os.FileInfo) bool {
	if identityEqualHook != nil {
		return identityEqualHook(first, second)
	}
	return os.SameFile(first, second)
}

// ValidateRoots is read-only. Write phases call EnsureState explicitly.
func ValidateRoots(envelope wire.Envelope) (Roots, error) {
	repository, err := captureDirectory(envelope.RepositoryRoot)
	if err != nil {
		return Roots{}, err
	}
	gitCapture, commonCapture, gitMetadataInfo, err := discoverGitDirectories(repository)
	if err != nil {
		repository.close()
		return Roots{}, err
	}
	state, err := captureState(envelope.StateRoot)
	if err != nil {
		closeCapturedDirectories(gitCapture, commonCapture)
		repository.close()
		return Roots{}, err
	}
	if stateOverlaps(repository, state) || stateOverlaps(gitCapture, state) || stateOverlaps(commonCapture, state) {
		closeCapturedDirectories(gitCapture, commonCapture)
		state.close()
		repository.close()
		return Roots{}, ErrRootOverlap
	}
	if state.root != nil {
		if err := ownerOnly(state.root); err != nil {
			closeCapturedDirectories(gitCapture, commonCapture)
			state.close()
			repository.close()
			return Roots{}, err
		}
	}
	return Roots{
		Repository: repository.path, State: state.path,
		GitDirectory: gitCapture.path, GitCommonDirectory: commonCapture.path,
		GitDir: gitCapture.path, GitCommonDir: commonCapture.path,
		repositoryRoot: repository.root,
		repositoryInfo: repository.lineage[len(repository.lineage)-1],
		stateRoot:      state.root,
		stateParent:    state.parent, stateParts: state.parts,
		gitDirectoryRoot: gitCapture.root,
		gitDirectoryInfo: gitCapture.lineage[len(gitCapture.lineage)-1],
		gitCommonRoot:    commonCapture.root,
		gitCommonInfo:    commonCapture.lineage[len(commonCapture.lineage)-1],
		gitMetadataInfo:  gitMetadataInfo,
		ioObservation:    &ioObservationState{},
	}, nil
}

// Close releases retained root capabilities. It is safe to call more than once.
func (r *Roots) Close() error {
	roots := []*os.Root{r.repositoryRoot, r.gitDirectoryRoot, r.gitCommonRoot, r.stateRoot, r.stateParent}
	r.repositoryRoot, r.gitDirectoryRoot, r.gitCommonRoot, r.stateRoot, r.stateParent = nil, nil, nil, nil, nil
	errs := closeUniqueRootCapabilities(roots...)
	return errors.Join(errs...)
}

// EnsureState creates state components through the captured parent handle.
func (r *Roots) EnsureState() error {
	if r.stateRoot != nil {
		return nil
	}
	if r.stateParent == nil || len(r.stateParts) == 0 {
		return ErrStateUnavailable
	}
	current := r.stateParent
	var opened []*os.Root
	success := false
	defer func() {
		if !success {
			closeRoots(opened)
		}
	}()
	for _, component := range r.stateParts {
		info, err := current.Lstat(component)
		if errors.Is(err, os.ErrNotExist) {
			if stateCreateHook != nil {
				stateCreateHook(component)
			}
			info, err = current.Lstat(component)
			if errors.Is(err, os.ErrNotExist) {
				err = current.Mkdir(component, 0o700)
				if err != nil && !errors.Is(err, os.ErrExist) {
					return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
				}
				info, err = current.Lstat(component)
			}
		}
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return ErrUnsafeRoot
		}
		if stateEnsureBeforeOpenHook != nil {
			stateEnsureBeforeOpenHook(component)
		}
		next, err := current.OpenRoot(component)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
		}
		openedInfo, statErr := next.Stat(".")
		latest, latestErr := current.Lstat(component)
		if statErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameIdentity(info, openedInfo) || !sameIdentity(info, latest) || !sameIdentity(openedInfo, latest) {
			_ = next.Close()
			return ErrUnsafeRoot
		}
		if err := r.rejectProtectedDirectory(openedInfo); err != nil {
			_ = next.Close()
			return err
		}
		opened = append(opened, next)
		current = next
	}
	if err := ownerOnly(current); err != nil {
		return err
	}
	_ = r.stateParent.Close()
	for _, intermediate := range opened[:len(opened)-1] {
		_ = intermediate.Close()
	}
	r.stateParent, r.stateRoot, r.stateParts = nil, current, nil
	success = true
	return nil
}

// OpenStateFile opens and validates an existing state file read-only.
func (r *Roots) OpenStateFile(relative string) (*os.File, error) {
	current, closers, name, err := r.stateFileLocation(relative)
	if err != nil {
		return nil, err
	}
	defer closeRoots(closers)
	before, err := current.Lstat(name)
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() {
		return nil, ErrUnsafeRoot
	}
	file, err := current.Open(name)
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	after, err := current.Lstat(name)
	opened, statErr := file.Stat()
	if err != nil || statErr != nil || after.Mode()&os.ModeSymlink != 0 || !sameSnapshot(before, after) || !sameSnapshot(before, opened) || safeStateFile(file) != nil {
		_ = file.Close()
		return nil, ErrUnsafeRoot
	}
	return file, nil
}

// CreateStateFile creates a new owner-only file without ever reopening or
// truncating an existing name.
func (r *Roots) CreateStateFile(relative string) (*os.File, error) {
	current, closers, name, err := r.stateFileLocation(relative)
	if err != nil {
		return nil, err
	}
	defer closeRoots(closers)
	if _, err := current.Lstat(name); err == nil {
		return nil, ErrUnsafeRoot
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, ErrUnsafeRoot
	}
	file, err := current.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	if safeStateFile(file) != nil {
		_ = file.Close()
		return nil, ErrUnsafeRoot
	}
	return file, nil
}

// ReplaceStateFile validates an existing destination before atomically
// replacing it with a synced owner-only file in the same retained directory.
func (r *Roots) ReplaceStateFile(relative string, contents []byte) error {
	current, closers, name, err := r.stateFileLocation(relative)
	if err != nil {
		return err
	}
	defer closeRoots(closers)
	if existing, err := current.Lstat(name); err == nil {
		if existing.Mode()&os.ModeSymlink != 0 || !existing.Mode().IsRegular() {
			return ErrUnsafeRoot
		}
		file, openErr := current.Open(name)
		if openErr != nil || safeStateFile(file) != nil {
			if file != nil {
				_ = file.Close()
			}
			return ErrUnsafeRoot
		}
		_ = file.Close()
	} else if !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	temporary := "." + name + ".next"
	if _, err := current.Lstat(temporary); err == nil {
		return ErrUnsafeRoot
	} else if !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	file, err := current.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return ErrUnsafeRoot
	}
	if safeStateFile(file) != nil {
		_ = file.Close()
		_ = current.Remove(temporary)
		return ErrUnsafeRoot
	}
	if _, err := file.Write(contents); err != nil {
		_ = file.Close()
		_ = current.Remove(temporary)
		return ErrUnsafeRoot
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		_ = current.Remove(temporary)
		return ErrUnsafeRoot
	}
	if err := file.Close(); err != nil {
		_ = current.Remove(temporary)
		return ErrUnsafeRoot
	}
	if err := current.Rename(temporary, name); err != nil {
		_ = current.Remove(temporary)
		return ErrUnsafeRoot
	}
	return nil
}

func (r *Roots) stateFileLocation(relative string) (*os.Root, []*os.Root, string, error) {
	if r.stateRoot == nil {
		return nil, nil, "", ErrStateUnavailable
	}
	components, err := safeComponents(relative, false)
	if err != nil {
		return nil, nil, "", err
	}
	current, closers, err := descendChecked(r.stateRoot, components[:len(components)-1], func(info os.FileInfo) error {
		return r.rejectProtectedDirectory(info)
	})
	if err != nil {
		return nil, nil, "", err
	}
	return current, closers, components[len(components)-1], nil
}

func (r *Roots) rejectProtectedDirectory(candidate os.FileInfo) error {
	protected := []struct {
		root     *os.Root
		identity os.FileInfo
	}{
		{root: r.repositoryRoot, identity: r.repositoryInfo},
		{root: r.gitDirectoryRoot, identity: r.gitDirectoryInfo},
		{root: r.gitCommonRoot, identity: r.gitCommonInfo},
	}
	for _, directory := range protected {
		if directory.root == nil || directory.identity == nil {
			return ErrUnsafeRoot
		}
		current, err := directory.root.Stat(".")
		if err != nil || !sameIdentity(current, directory.identity) {
			return ErrUnsafeRoot
		}
		if sameIdentity(candidate, directory.identity) || sameIdentity(candidate, current) {
			return ErrRootOverlap
		}
	}
	return nil
}

type capturedDirectory struct {
	path    string
	root    *os.Root
	lineage []os.FileInfo
}

func (c capturedDirectory) close() {
	if c.root != nil {
		_ = c.root.Close()
	}
}

func closeCapturedDirectories(captures ...capturedDirectory) {
	roots := make([]*os.Root, 0, len(captures))
	for _, capture := range captures {
		roots = append(roots, capture.root)
	}
	_ = errors.Join(closeUniqueRootCapabilities(roots...)...)
}

func closeUniqueRootCapabilities(roots ...*os.Root) []error {
	seen := make(map[*os.Root]struct{}, len(roots))
	var errs []error
	for _, root := range roots {
		if root == nil {
			continue
		}
		if _, duplicate := seen[root]; duplicate {
			continue
		}
		seen[root] = struct{}{}
		errs = append(errs, root.Close())
	}
	return errs
}

func captureDirectory(value string) (capturedDirectory, error) {
	path, err := absolutePath(value)
	if err != nil {
		return capturedDirectory{}, err
	}
	root, parts, err := openFilesystemRoot(path)
	if err != nil {
		return capturedDirectory{}, err
	}
	current := root
	rootInfo, err := root.Stat(".")
	if err != nil {
		_ = root.Close()
		return capturedDirectory{}, ErrUnsafeRoot
	}
	lineage := []os.FileInfo{rootInfo}
	for _, component := range parts {
		before, err := current.Lstat(component)
		if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.IsDir() {
			_ = current.Close()
			return capturedDirectory{}, ErrUnsafeRoot
		}
		nextPath := filepath.Join(current.Name(), component)
		if captureBeforeOpenHook != nil {
			captureBeforeOpenHook(nextPath)
		}
		next, err := current.OpenRoot(component)
		if err != nil {
			_ = current.Close()
			return capturedDirectory{}, ErrUnsafeRoot
		}
		opened, openErr := next.Stat(".")
		latest, latestErr := current.Lstat(component)
		_ = current.Close()
		if openErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameIdentity(before, opened) || !sameIdentity(before, latest) || !sameIdentity(opened, latest) {
			_ = next.Close()
			return capturedDirectory{}, ErrUnsafeRoot
		}
		current = next
		lineage = append(lineage, opened)
	}
	return capturedDirectory{path: path, root: current, lineage: lineage}, nil
}

type capturedState struct {
	path    string
	root    *os.Root
	parent  *os.Root
	parts   []string
	lineage []os.FileInfo
}

func (s capturedState) close() {
	if s.root != nil {
		_ = s.root.Close()
	}
	if s.parent != nil {
		_ = s.parent.Close()
	}
}

func captureState(value string) (capturedState, error) {
	path, err := absolutePath(value)
	if err != nil {
		return capturedState{}, err
	}
	root, parts, err := openFilesystemRoot(path)
	if err != nil {
		return capturedState{}, err
	}
	current := root
	rootInfo, err := root.Stat(".")
	if err != nil {
		_ = root.Close()
		return capturedState{}, ErrUnsafeRoot
	}
	lineage := []os.FileInfo{rootInfo}
	for index, component := range parts {
		before, err := current.Lstat(component)
		if errors.Is(err, os.ErrNotExist) {
			return capturedState{path: path, parent: current, parts: parts[index:], lineage: lineage}, nil
		}
		if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.IsDir() {
			_ = current.Close()
			return capturedState{}, ErrUnsafeRoot
		}
		nextPath := filepath.Join(current.Name(), component)
		if captureBeforeOpenHook != nil {
			captureBeforeOpenHook(nextPath)
		}
		next, err := current.OpenRoot(component)
		if err != nil {
			_ = current.Close()
			return capturedState{}, ErrUnsafeRoot
		}
		opened, openErr := next.Stat(".")
		latest, latestErr := current.Lstat(component)
		_ = current.Close()
		if openErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameIdentity(before, opened) || !sameIdentity(before, latest) || !sameIdentity(opened, latest) {
			_ = next.Close()
			return capturedState{}, ErrUnsafeRoot
		}
		current = next
		lineage = append(lineage, opened)
	}
	return capturedState{path: path, root: current, lineage: lineage}, nil
}

func stateOverlaps(directory capturedDirectory, state capturedState) bool {
	if len(directory.lineage) == 0 || len(state.lineage) == 0 {
		return true
	}
	directoryTerminal := directory.lineage[len(directory.lineage)-1]
	if infoInLineage(directoryTerminal, state.lineage) {
		return true
	}
	if state.root != nil {
		return infoInLineage(state.lineage[len(state.lineage)-1], directory.lineage)
	}
	return false
}

func infoInLineage(needle os.FileInfo, lineage []os.FileInfo) bool {
	for _, candidate := range lineage {
		if sameIdentity(needle, candidate) {
			return true
		}
	}
	return false
}

func openFilesystemRoot(path string) (*os.Root, []string, error) {
	volume := filepath.VolumeName(path)
	rootPath := volume + string(filepath.Separator)
	remainder := strings.TrimPrefix(path, rootPath)
	if remainder == path {
		return nil, nil, ErrUnsafeRoot
	}
	root, err := os.OpenRoot(rootPath)
	if err != nil {
		return nil, nil, ErrUnsafeRoot
	}
	if remainder == "" {
		return root, nil, nil
	}
	return root, strings.Split(remainder, string(filepath.Separator)), nil
}

func absolutePath(value string) (string, error) {
	if value == "" || !filepath.IsAbs(value) {
		return "", ErrUnsafeRoot
	}
	return filepath.Clean(value), nil
}

func discoverGitDirectories(repository capturedDirectory) (capturedDirectory, capturedDirectory, os.FileInfo, error) {
	info, err := repository.root.Lstat(".git")
	if err != nil {
		return capturedDirectory{}, capturedDirectory{}, nil, fmt.Errorf("%w: missing Git metadata", ErrUnsafeRoot)
	}
	if info.Mode()&os.ModeSymlink != 0 || (!info.IsDir() && !info.Mode().IsRegular()) {
		return capturedDirectory{}, capturedDirectory{}, nil, ErrUnsafeRoot
	}
	if gitDiscoveryBeforeOpenHook != nil {
		gitDiscoveryBeforeOpenHook()
	}
	var git capturedDirectory
	switch {
	case info.IsDir():
		git, err = captureDirectoryEntry(repository, ".git", info)
	case info.Mode().IsRegular():
		var value string
		value, err = readGitDirMetadata(repository.root, info)
		if err != nil {
			return capturedDirectory{}, capturedDirectory{}, nil, err
		}
		if metadataTargetCaptureHook != nil {
			metadataTargetCaptureHook(".git")
		}
		git, err = captureMetadataDirectory(repository, value)
	}
	if err != nil {
		return capturedDirectory{}, capturedDirectory{}, nil, err
	}
	common := git
	if commonInfo, commonErr := git.root.Lstat("commondir"); commonErr == nil {
		if commonInfo.Mode()&os.ModeSymlink != 0 || !commonInfo.Mode().IsRegular() {
			git.close()
			return capturedDirectory{}, capturedDirectory{}, nil, ErrUnsafeRoot
		}
		value, err := readCommonDirMetadata(git.root, commonInfo)
		if err != nil {
			git.close()
			return capturedDirectory{}, capturedDirectory{}, nil, err
		}
		if metadataTargetCaptureHook != nil {
			metadataTargetCaptureHook("commondir")
		}
		common, err = captureMetadataDirectory(git, value)
		if err != nil {
			git.close()
			return capturedDirectory{}, capturedDirectory{}, nil, err
		}
	} else if !errors.Is(commonErr, os.ErrNotExist) {
		git.close()
		return capturedDirectory{}, capturedDirectory{}, nil, ErrUnsafeRoot
	}
	return git, common, info, nil
}

// captureMetadataDirectory binds relative metadata to the retained base
// capability. Targets beneath the base are opened descriptor-relative. A
// target containing enough ".." components to escape the base requires a
// lexical absolute path, so the lexical base is recaptured and compared
// componentwise with the retained lineage immediately before and after the
// target capture.
func captureMetadataDirectory(base capturedDirectory, value string) (capturedDirectory, error) {
	if filepath.IsAbs(value) {
		return captureDirectory(value)
	}
	if filepath.VolumeName(value) != "" {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	targetPath, err := resolveMetadataPath(base.path, value)
	if err != nil {
		return capturedDirectory{}, err
	}
	if err := verifyCapturedDirectoryPath(base); err != nil {
		return capturedDirectory{}, err
	}
	cleanRelative := filepath.Clean(value)
	var target capturedDirectory
	if metadataPathStaysWithinBase(cleanRelative) {
		target, err = captureRelativeDirectory(base, cleanRelative)
	} else {
		target, err = captureDirectory(targetPath)
	}
	if err != nil {
		return capturedDirectory{}, err
	}
	if err := verifyCapturedDirectoryPath(base); err != nil {
		target.close()
		return capturedDirectory{}, err
	}
	return target, nil
}

func metadataPathStaysWithinBase(relative string) bool {
	return relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative)
}

func captureRelativeDirectory(base capturedDirectory, relative string) (capturedDirectory, error) {
	if relative == "." {
		return duplicateCapturedDirectory(base)
	}
	current := base
	owned := false
	for _, component := range strings.Split(relative, string(filepath.Separator)) {
		before, err := current.root.Lstat(component)
		if err != nil {
			if owned {
				current.close()
			}
			return capturedDirectory{}, ErrUnsafeRoot
		}
		next, err := captureDirectoryEntry(current, component, before)
		if owned {
			current.close()
		}
		if err != nil {
			return capturedDirectory{}, err
		}
		current, owned = next, true
	}
	return current, nil
}

func duplicateCapturedDirectory(directory capturedDirectory) (capturedDirectory, error) {
	if directory.root == nil || len(directory.lineage) == 0 {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	before, err := directory.root.Stat(".")
	if err != nil || !sameIdentity(before, directory.lineage[len(directory.lineage)-1]) {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	next, err := directory.root.OpenRoot(".")
	if err != nil {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	opened, openErr := next.Stat(".")
	after, afterErr := directory.root.Stat(".")
	if openErr != nil || afterErr != nil || !sameIdentity(before, opened) || !sameIdentity(before, after) || !sameIdentity(opened, after) {
		_ = next.Close()
		return capturedDirectory{}, ErrUnsafeRoot
	}
	return capturedDirectory{path: directory.path, root: next, lineage: append([]os.FileInfo(nil), directory.lineage...)}, nil
}

func verifyCapturedDirectoryPath(expected capturedDirectory) error {
	if expected.root == nil || len(expected.lineage) == 0 {
		return ErrUnsafeRoot
	}
	retained, err := expected.root.Stat(".")
	if err != nil || !sameIdentity(retained, expected.lineage[len(expected.lineage)-1]) {
		return ErrUnsafeRoot
	}
	lexical, err := captureDirectory(expected.path)
	if err != nil {
		return ErrUnsafeRoot
	}
	defer lexical.close()
	if len(lexical.lineage) != len(expected.lineage) {
		return ErrUnsafeRoot
	}
	for index := range expected.lineage {
		if !sameIdentity(lexical.lineage[index], expected.lineage[index]) {
			return ErrUnsafeRoot
		}
	}
	lexicalTerminal, err := lexical.root.Stat(".")
	if err != nil || !sameIdentity(lexicalTerminal, retained) {
		return ErrUnsafeRoot
	}
	return nil
}

func captureDirectoryEntry(parent capturedDirectory, name string, before os.FileInfo) (capturedDirectory, error) {
	if before.Mode()&os.ModeSymlink != 0 || !before.IsDir() {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	path := filepath.Join(parent.path, name)
	if captureBeforeOpenHook != nil {
		captureBeforeOpenHook(path)
	}
	next, err := parent.root.OpenRoot(name)
	if err != nil {
		return capturedDirectory{}, ErrUnsafeRoot
	}
	opened, openErr := next.Stat(".")
	latest, latestErr := parent.root.Lstat(name)
	if openErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameIdentity(before, opened) || !sameIdentity(before, latest) || !sameIdentity(opened, latest) {
		_ = next.Close()
		return capturedDirectory{}, ErrUnsafeRoot
	}
	lineage := append([]os.FileInfo(nil), parent.lineage...)
	lineage = append(lineage, opened)
	return capturedDirectory{path: path, root: next, lineage: lineage}, nil
}

func readGitDirMetadata(root *os.Root, expected os.FileInfo) (string, error) {
	text, err := readStableMetadata(root, ".git", expected)
	if err != nil || !strings.HasPrefix(text, "gitdir: ") {
		return "", ErrUnsafeRoot
	}
	return plainMetadataPath(strings.TrimPrefix(text, "gitdir: "))
}

func readCommonDirMetadata(root *os.Root, expected os.FileInfo) (string, error) {
	text, err := readStableMetadata(root, "commondir", expected)
	if err != nil || strings.HasPrefix(text, "gitdir: ") {
		return "", ErrUnsafeRoot
	}
	return plainMetadataPath(text)
}

func readStableMetadata(root *os.Root, name string, before os.FileInfo) (string, error) {
	if before == nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() {
		return "", ErrUnsafeRoot
	}
	if metadataOpenHook != nil {
		metadataOpenHook(name)
	}
	file, err := root.Open(name)
	if err != nil {
		return "", ErrUnsafeRoot
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameSnapshot(before, opened) {
		return "", ErrUnsafeRoot
	}
	contents, err := io.ReadAll(io.LimitReader(file, maximumGitMetadataBytes+1))
	after, afterErr := root.Lstat(name)
	if err != nil || afterErr != nil || after.Mode()&os.ModeSymlink != 0 || !after.Mode().IsRegular() || !sameSnapshot(before, after) {
		return "", ErrUnsafeRoot
	}
	if len(contents) == 0 || len(contents) > maximumGitMetadataBytes || !utf8.Valid(contents) || strings.ContainsRune(string(contents), '\x00') {
		return "", ErrUnsafeRoot
	}
	return string(contents), nil
}

func plainMetadataPath(text string) (string, error) {
	text = strings.TrimSuffix(text, "\n")
	if text == "" || strings.ContainsAny(text, "\r\n") {
		return "", ErrUnsafeRoot
	}
	return text, nil
}

func resolveMetadataPath(base, value string) (string, error) {
	if !filepath.IsAbs(value) {
		value = filepath.Join(base, value)
	}
	return absolutePath(value)
}

func overlaps(first, second string) bool { return contains(first, second) || contains(second, first) }

func contains(parent, child string) bool {
	relative, err := filepath.Rel(parent, child)
	if err != nil {
		return true
	}
	return relative == "." || (relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative))
}
