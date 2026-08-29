package boundary

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode"
)

type StableFile struct {
	RelativePath string
	Bytes        []byte
	SHA256       string
	Size         int64
}

// repositoryOpenHook provides a deterministic adversarial stat/open seam for
// boundary tests. Production leaves it nil.
var repositoryOpenHook func()
var repositoryBeforeReadHook func()
var descendBeforeOpenHook func(component string)

func (r *Roots) OpenRepositoryFile(relative string, maximum int64) (StableFile, error) {
	components, err := safeComponents(relative, true)
	if err != nil || r.repositoryRoot == nil {
		return StableFile{}, ErrUnsafePath
	}
	if maximum < 0 {
		return StableFile{}, ErrFileTooLarge
	}
	if protectedRepositoryPath(r, relative) {
		return StableFile{}, ErrUnsafePath
	}
	current, closers, err := descend(r.repositoryRoot, components[:len(components)-1])
	if err != nil {
		return StableFile{}, err
	}
	defer closeRoots(closers)
	if r.entersGitDirectory(append(closers, current)) {
		return StableFile{}, ErrUnsafePath
	}
	name := components[len(components)-1]
	before, err := current.Lstat(name)
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() {
		return StableFile{}, ErrUnsafePath
	}
	if r.gitMetadataInfo != nil && sameIdentity(before, r.gitMetadataInfo) {
		return StableFile{}, ErrUnsafePath
	}
	if before.Size() > maximum {
		return StableFile{}, ErrFileTooLarge
	}
	if repositoryOpenHook != nil {
		repositoryOpenHook()
	}
	file, err := current.Open(name)
	if err != nil {
		return StableFile{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	r.observeIO(IOObservation{FullBodyOpens: 1})
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameSnapshot(before, opened) {
		return StableFile{}, ErrUnstableFile
	}
	if repositoryBeforeReadHook != nil {
		repositoryBeforeReadHook()
	}
	contents, err := readAtMost(file, maximum, func(count int) {
		r.observeIO(IOObservation{FullBodyBytes: uint64(count)})
	})
	if err != nil {
		return StableFile{}, err
	}
	after, err := file.Stat()
	pathAfter, pathErr := current.Lstat(name)
	if err != nil || pathErr != nil || pathAfter.Mode()&os.ModeSymlink != 0 || !pathAfter.Mode().IsRegular() || !sameSnapshot(before, after) || !sameSnapshot(before, pathAfter) {
		return StableFile{}, ErrUnstableFile
	}
	digest := sha256.Sum256(contents)
	return StableFile{RelativePath: relative, Bytes: contents, SHA256: hex.EncodeToString(digest[:]), Size: int64(len(contents))}, nil
}

// OpenGitMetadataFile reads a regular Git-control file through the retained
// worktree Git-directory capability. It never consults a pathname root.
func (r *Roots) OpenGitMetadataFile(relative string, maximum int64) (StableFile, error) {
	return r.openGitMetadataFile(r.gitDirectoryRoot, relative, maximum, false)
}

// OpenGitCommonMetadataFile reads through the retained common Git directory.
// Its bytes count as bounded policy-prefix I/O rather than repository bodies.
func (r *Roots) OpenGitCommonMetadataFile(relative string, maximum int64) (StableFile, error) {
	return r.openGitMetadataFile(r.gitCommonRoot, relative, maximum, true)
}

func (r *Roots) openGitMetadataFile(root *os.Root, relative string, maximum int64, observePrefix bool) (StableFile, error) {
	components, err := safeComponents(relative, false)
	if err != nil || root == nil || maximum < 0 {
		return StableFile{}, ErrUnsafePath
	}
	current, closers, err := descendGitMetadata(root, components[:len(components)-1])
	if err != nil {
		return StableFile{}, err
	}
	defer closeRoots(closers)
	name := components[len(components)-1]
	before, err := current.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return StableFile{}, ErrGitMetadataNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() || before.Size() > maximum {
		return StableFile{}, ErrUnsafePath
	}
	file, err := current.Open(name)
	if err != nil {
		return StableFile{}, fmt.Errorf("%w: %v", ErrUnsafePath, err)
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameSnapshot(before, opened) {
		return StableFile{}, ErrUnstableFile
	}
	var observe func(int)
	if observePrefix {
		observe = func(count int) { r.observeIO(IOObservation{ReadPrefixBytes: uint64(count)}) }
	}
	contents, err := readAtMost(file, maximum, observe)
	if err != nil {
		return StableFile{}, err
	}
	after, err := file.Stat()
	pathAfter, pathErr := current.Lstat(name)
	if err != nil || pathErr != nil || pathAfter.Mode()&os.ModeSymlink != 0 || !pathAfter.Mode().IsRegular() || !sameSnapshot(before, after) || !sameSnapshot(before, pathAfter) {
		return StableFile{}, ErrUnstableFile
	}
	digest := sha256.Sum256(contents)
	return StableFile{RelativePath: relative, Bytes: contents, SHA256: hex.EncodeToString(digest[:]), Size: int64(len(contents))}, nil
}

func descendGitMetadata(root *os.Root, components []string) (*os.Root, []*os.Root, error) {
	current := root
	var closers []*os.Root
	for _, component := range components {
		info, err := current.Lstat(component)
		if errors.Is(err, os.ErrNotExist) {
			closeRoots(closers)
			return nil, nil, ErrGitMetadataNotFound
		}
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		next, err := current.OpenRoot(component)
		if err != nil {
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		opened, openErr := next.Stat(".")
		latest, latestErr := current.Lstat(component)
		if openErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameSnapshot(info, opened) || !sameSnapshot(info, latest) {
			_ = next.Close()
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		closers, current = append(closers, next), next
	}
	return current, closers, nil
}

func safeComponents(relative string, rejectGit bool) ([]string, error) {
	if relative == "" || strings.ContainsAny(relative, "\\\x00") || path.IsAbs(relative) || filepath.IsAbs(relative) || hasDrivePrefix(relative) || path.Clean(relative) != relative {
		return nil, ErrUnsafePath
	}
	components := strings.Split(relative, "/")
	for _, component := range components {
		if component == "" || component == "." || component == ".." || (rejectGit && strings.EqualFold(component, ".git")) {
			return nil, ErrUnsafePath
		}
	}
	return components, nil
}

func descend(root *os.Root, components []string) (*os.Root, []*os.Root, error) {
	return descendChecked(root, components, nil)
}

func descendChecked(root *os.Root, components []string, validate func(os.FileInfo) error) (*os.Root, []*os.Root, error) {
	current := root
	var closers []*os.Root
	for _, component := range components {
		info, err := current.Lstat(component)
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		if descendBeforeOpenHook != nil {
			descendBeforeOpenHook(component)
		}
		next, err := current.OpenRoot(component)
		if err != nil {
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		opened, openErr := next.Stat(".")
		latest, latestErr := current.Lstat(component)
		if openErr != nil || latestErr != nil || latest.Mode()&os.ModeSymlink != 0 || !sameIdentity(info, opened) || !sameIdentity(info, latest) || !sameIdentity(opened, latest) {
			_ = next.Close()
			closeRoots(closers)
			return nil, nil, ErrUnsafePath
		}
		if validate != nil {
			if err := validate(opened); err != nil {
				_ = next.Close()
				closeRoots(closers)
				return nil, nil, err
			}
		}
		closers, current = append(closers, next), next
	}
	return current, closers, nil
}

func closeRoots(roots []*os.Root) {
	for index := len(roots) - 1; index >= 0; index-- {
		_ = roots[index].Close()
	}
}

func protectedRepositoryPath(roots *Roots, relative string) bool {
	target := filepath.Join(roots.Repository, filepath.FromSlash(relative))
	return contains(roots.GitDirectory, target) || contains(roots.GitCommonDirectory, target)
}

// entersGitDirectory compares the retained identities of every directory
// traversed for a read. This catches case-insensitive aliases and descendants
// of a separate Git directory, where lexical spelling is not authoritative.
func (r *Roots) entersGitDirectory(roots []*os.Root) bool {
	for _, root := range roots {
		info, err := root.Stat(".")
		if err != nil {
			return true
		}
		if (r.gitDirectoryInfo != nil && sameIdentity(info, r.gitDirectoryInfo)) || (r.gitCommonInfo != nil && sameIdentity(info, r.gitCommonInfo)) {
			return true
		}
	}
	return false
}

func hasDrivePrefix(value string) bool {
	return len(value) >= 2 && value[1] == ':' && unicode.IsLetter(rune(value[0]))
}
func sameSnapshot(first, second os.FileInfo) bool {
	return os.SameFile(first, second) && first.Mode() == second.Mode() && first.Size() == second.Size() && first.ModTime().Equal(second.ModTime())
}

func readAtMost(file *os.File, maximum int64, observe func(int)) ([]byte, error) {
	var contents []byte
	var buffer [32 * 1024]byte
	for total := int64(0); ; {
		if total == maximum {
			var extra [1]byte
			n, err := file.Read(extra[:])
			if n != 0 {
				if observe != nil {
					observe(n)
				}
				return nil, ErrFileTooLarge
			}
			if err == io.EOF {
				return contents, nil
			}
			if err != nil {
				return nil, fmt.Errorf("%w: %v", ErrUnsafePath, err)
			}
			continue
		}
		remaining, readSize := maximum-total, int64(len(buffer))
		if remaining < readSize {
			readSize = remaining
		}
		if readSize > math.MaxInt {
			readSize = math.MaxInt
		}
		n, err := file.Read(buffer[:int(readSize)])
		if n > 0 {
			if observe != nil {
				observe(n)
			}
			contents, total = append(contents, buffer[:n]...), total+int64(n)
		}
		if err == io.EOF {
			return contents, nil
		}
		if err != nil {
			return nil, fmt.Errorf("%w: %v", ErrUnsafePath, err)
		}
	}
}
