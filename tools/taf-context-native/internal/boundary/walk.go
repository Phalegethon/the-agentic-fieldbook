package boundary

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strings"
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

// directoryReadHook provides a deterministic mutation seam for boundary
// tests. Production leaves it nil.
var directoryReadHook func()

// WalkRepository enumerates repository metadata through the captured root
// capability. It never follows symlinks and never descends into Git metadata.
func (r *Roots) WalkRepository(visit func(RepositoryEntry) error) error {
	if r.repositoryRoot == nil || visit == nil {
		return ErrUnsafePath
	}
	return r.walkRepositoryDirectory(r.repositoryRoot, "", nil, visit)
}

func (r *Roots) walkRepositoryDirectory(current *os.Root, parent string, ancestors []*os.Root, visit func(RepositoryEntry) error) error {
	if r.entersGitDirectory(append(ancestors, current)) {
		return ErrUnsafePath
	}
	entries, err := readRootDirectory(current)
	if err != nil {
		return err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	for _, entry := range entries {
		name := entry.Name()
		if _, err := safeComponents(name, false); err != nil {
			return ErrUnsafePath
		}
		info, err := current.Lstat(name)
		if err != nil {
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
		err = r.walkRepositoryDirectory(next, relative, append(ancestors, current), visit)
		closeErr := next.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return fmt.Errorf("%w: %v", ErrUnsafePath, closeErr)
		}
	}
	return nil
}

func (r *Roots) isGitMetadata(info os.FileInfo) bool {
	return (r.gitDirectoryInfo != nil && sameIdentity(info, r.gitDirectoryInfo)) ||
		(r.gitCommonInfo != nil && sameIdentity(info, r.gitCommonInfo)) ||
		(r.gitMetadataInfo != nil && sameIdentity(info, r.gitMetadataInfo))
}

func readRootDirectory(root *os.Root) ([]os.DirEntry, error) {
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
	entries, err := directory.ReadDir(-1)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	if directoryReadHook != nil {
		directoryReadHook()
	}
	afterRead, afterReadErr := root.Stat(".")
	if afterReadErr != nil || !sameSnapshot(before, afterRead) || !sameSnapshot(opened, afterRead) {
		return nil, ErrUnsafePath
	}
	return entries, nil
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
