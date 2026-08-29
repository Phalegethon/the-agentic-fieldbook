package boundary

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strings"
	"sync"
)

// RepositoryEntry is metadata observed through the retained repository
// capability. RelativePath always uses slash separators.
type RepositoryEntry struct {
	RelativePath string
	Mode         os.FileMode
	Size         int64
	GitMetadata  bool
}

// StablePrefix is a post-validated bounded read of a regular repository file.
// Size is the complete file size, while Bytes has at most the requested limit.
type StablePrefix struct {
	RelativePath string
	Bytes        []byte
	Size         int64
}

// IOObservation reports repository I/O performed through this retained root.
// Copies of Roots share the same monotonic observation state.
type IOObservation struct {
	ReadDirectoryEntries int
	ReadPrefixBytes      uint64
	FullBodyOpens        int
	FullBodyBytes        uint64
}

type ioObservationState struct {
	mu    sync.Mutex
	value IOObservation
}

func (r *Roots) observeIO(delta IOObservation) {
	if r.ioObservation == nil {
		return
	}
	r.ioObservation.mu.Lock()
	r.ioObservation.value.ReadDirectoryEntries += delta.ReadDirectoryEntries
	r.ioObservation.value.ReadPrefixBytes += delta.ReadPrefixBytes
	r.ioObservation.value.FullBodyOpens += delta.FullBodyOpens
	r.ioObservation.value.FullBodyBytes += delta.FullBodyBytes
	r.ioObservation.mu.Unlock()
	if repositoryIOHook != nil {
		repositoryIOHook(delta)
	}
}

// IOObservation returns an atomic snapshot of repository I/O observations.
func (r *Roots) IOObservation() IOObservation {
	if r.ioObservation == nil {
		return IOObservation{}
	}
	r.ioObservation.mu.Lock()
	defer r.ioObservation.mu.Unlock()
	return r.ioObservation.value
}

// directoryReadHook provides a deterministic mutation seam for boundary
// tests. Production leaves it nil.
var directoryReadHook func()
var repositoryIOHook func(IOObservation)

const maximumDirectoryBatch = 256

type repositoryWalkBudget struct {
	remaining int
}

type repositorySnapshotEntry struct {
	name string
	info os.FileInfo
}

// WalkRepository enumerates repository metadata through the captured root
// capability. It never follows symlinks and never descends into Git metadata.
func (r *Roots) WalkRepository(maximumObservations int, visit func(RepositoryEntry) error) error {
	if r.repositoryRoot == nil || visit == nil {
		return ErrUnsafePath
	}
	if maximumObservations <= 0 {
		return ErrRepositoryEnumerationLimit
	}
	budget := &repositoryWalkBudget{remaining: maximumObservations}
	return r.walkRepositoryDirectory(r.repositoryRoot, "", nil, budget, visit)
}

func (r *Roots) walkRepositoryDirectory(current *os.Root, parent string, ancestors []*os.Root, budget *repositoryWalkBudget, visit func(RepositoryEntry) error) error {
	if r.entersGitDirectory(append(ancestors, current)) {
		return ErrUnsafePath
	}
	entries, err := r.readRootDirectory(current, budget, true)
	if err != nil {
		return err
	}
	sort.Slice(entries, func(i, j int) bool {
		leftIgnore, rightIgnore := entries[i].name == ".gitignore", entries[j].name == ".gitignore"
		if leftIgnore != rightIgnore {
			return leftIgnore
		}
		return entries[i].name < entries[j].name
	})
	for _, entry := range entries {
		name := entry.name
		if _, err := safeComponents(name, false); err != nil {
			return ErrUnsafePath
		}
		info, err := current.Lstat(name)
		if err != nil || !sameSnapshot(entry.info, info) {
			return fmt.Errorf("%w: %v", ErrUnsafePath, err)
		}
		relative := name
		if parent != "" {
			relative = path.Join(parent, name)
		}
		isGitMetadata := r.isGitMetadata(info)
		if err := visit(RepositoryEntry{RelativePath: relative, Mode: info.Mode(), Size: info.Size(), GitMetadata: isGitMetadata}); err != nil {
			if errors.Is(err, ErrSkipRepositoryDirectory) && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
				continue
			}
			return err
		}
		// A .git component is repository metadata even for nested repositories.
		if isGitMetadata || strings.EqualFold(name, ".git") || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			continue
		}
		if descendBeforeOpenHook != nil {
			descendBeforeOpenHook(name)
		}
		next, err := current.OpenRoot(name)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrUnsafePath, err)
		}
		opened, statErr := next.Stat(".")
		latest, latestErr := current.Lstat(name)
		if statErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !latest.IsDir() || !sameIdentity(info, opened) || !sameIdentity(info, latest) || !sameIdentity(opened, latest) {
			_ = next.Close()
			return ErrUnsafePath
		}
		if r.entersGitDirectory(append(append(ancestors, current), next)) {
			_ = next.Close()
			continue
		}
		err = r.walkRepositoryDirectory(next, relative, append(ancestors, current), budget, visit)
		closeErr := next.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return fmt.Errorf("%w: %v", ErrUnsafePath, closeErr)
		}
	}
	finalEntries, err := r.readRootDirectory(current, budget, false)
	if err != nil {
		return err
	}
	if !sameRepositorySnapshot(entries, finalEntries) {
		return ErrRepositoryChanged
	}
	return nil
}

func (r *Roots) isGitMetadata(info os.FileInfo) bool {
	return (r.gitDirectoryInfo != nil && sameIdentity(info, r.gitDirectoryInfo)) ||
		(r.gitCommonInfo != nil && sameIdentity(info, r.gitCommonInfo)) ||
		(r.gitMetadataInfo != nil && sameIdentity(info, r.gitMetadataInfo))
}

func (r *Roots) readRootDirectory(root *os.Root, budget *repositoryWalkBudget, invokeHook bool) ([]repositorySnapshotEntry, error) {
	before, err := root.Stat(".")
	if err != nil || !before.IsDir() {
		return nil, ErrUnsafePath
	}
	directory, err := root.Open(".")
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	defer directory.Close()
	opened, err := directory.Stat()
	after, afterErr := root.Stat(".")
	if err != nil || afterErr != nil || !opened.IsDir() || !sameIdentity(before, opened) || !sameIdentity(before, after) || !sameIdentity(opened, after) {
		return nil, ErrUnsafePath
	}
	var entries []repositorySnapshotEntry
	for {
		if budget.remaining == 0 {
			return nil, ErrRepositoryEnumerationLimit
		}
		batchMaximum := maximumDirectoryBatch
		if budget.remaining < batchMaximum {
			batchMaximum = budget.remaining
		}
		batch, readErr := directory.ReadDir(batchMaximum)
		budget.remaining -= len(batch)
		r.observeIO(IOObservation{ReadDirectoryEntries: len(batch)})
		for _, entry := range batch {
			info, statErr := root.Lstat(entry.Name())
			if statErr != nil {
				return nil, fmt.Errorf("%w: %v", ErrUnsafePath, statErr)
			}
			entries = append(entries, repositorySnapshotEntry{name: entry.Name(), info: info})
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return nil, fmt.Errorf("%w: %v", ErrUnsafePath, readErr)
		}
		if len(batch) == 0 {
			return nil, ErrUnsafePath
		}
	}
	if invokeHook && directoryReadHook != nil {
		directoryReadHook()
	}
	afterRead, afterReadErr := root.Stat(".")
	if afterReadErr != nil || !sameSnapshot(before, afterRead) || !sameSnapshot(opened, afterRead) {
		return nil, ErrUnsafePath
	}
	return entries, nil
}

func sameRepositorySnapshot(first, second []repositorySnapshotEntry) bool {
	if len(first) != len(second) {
		return false
	}
	firstByName := make(map[string]os.FileInfo, len(first))
	for _, entry := range first {
		firstByName[entry.name] = entry.info
	}
	for _, entry := range second {
		previous, ok := firstByName[entry.name]
		if !ok || !sameSnapshot(previous, entry.info) {
			return false
		}
	}
	return true
}

// ReadRepositoryPrefix reads no more than maximum bytes from a regular file
// through the retained repository capability. The file is identity-checked
// before opening, after opening, and after the read.
func (r *Roots) ReadRepositoryPrefix(relative string, maximum int64) (StablePrefix, error) {
	components, err := safeComponents(relative, true)
	if err != nil || r.repositoryRoot == nil || maximum < 0 || protectedRepositoryPath(r, relative) {
		return StablePrefix{}, ErrUnsafePath
	}
	current, closers, err := descend(r.repositoryRoot, components[:len(components)-1])
	if err != nil {
		return StablePrefix{}, err
	}
	defer closeRoots(closers)
	if r.entersGitDirectory(append(closers, current)) {
		return StablePrefix{}, ErrUnsafePath
	}
	name := components[len(components)-1]
	before, err := current.Lstat(name)
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() || (r.gitMetadataInfo != nil && sameIdentity(before, r.gitMetadataInfo)) {
		return StablePrefix{}, ErrUnsafePath
	}
	if repositoryOpenHook != nil {
		repositoryOpenHook()
	}
	file, err := current.Open(name)
	if err != nil {
		return StablePrefix{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameSnapshot(before, opened) {
		return StablePrefix{}, ErrUnstableFile
	}
	contents, err := io.ReadAll(io.LimitReader(file, maximum))
	r.observeIO(IOObservation{ReadPrefixBytes: uint64(len(contents))})
	if err != nil {
		return StablePrefix{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	after, err := file.Stat()
	pathAfter, pathErr := current.Lstat(name)
	if err != nil || pathErr != nil || pathAfter.Mode()&os.ModeSymlink != 0 || !pathAfter.Mode().IsRegular() || !sameSnapshot(before, after) || !sameSnapshot(before, pathAfter) {
		return StablePrefix{}, ErrUnstableFile
	}
	return StablePrefix{RelativePath: relative, Bytes: contents, Size: before.Size()}, nil
}
