package boundary

import (
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
)

// StateDirectory is an owner-only descriptor-relative state capability.
// It never resolves the lexical state-root path captured in Roots.
type StateDirectory struct {
	root  *os.Root
	roots *Roots
}

// OpenStateDirectory duplicates the retained state capability and descends
// only through validated owner-only directory handles.
func (r *Roots) OpenStateDirectory(relative string) (*StateDirectory, error) {
	if r == nil || r.stateRoot == nil {
		return nil, ErrStateUnavailable
	}
	current, err := duplicateStateRoot(r.stateRoot, r)
	if err != nil {
		return nil, err
	}
	if relative == "" {
		return &StateDirectory{root: current, roots: r}, nil
	}
	components, err := safeComponents(relative, false)
	if err != nil {
		_ = current.Close()
		return nil, ErrUnsafeRoot
	}
	for _, component := range components {
		directory := &StateDirectory{root: current, roots: r}
		next, err := directory.OpenDirectory(component)
		_ = current.Close()
		if err != nil {
			return nil, err
		}
		current = next.root
	}
	return &StateDirectory{root: current, roots: r}, nil
}

func duplicateStateRoot(root *os.Root, roots *Roots) (*os.Root, error) {
	before, err := root.Stat(".")
	if err != nil || !before.IsDir() || ownerOnly(root) != nil || roots.rejectProtectedDirectory(before) != nil {
		return nil, ErrUnsafeRoot
	}
	next, err := root.OpenRoot(".")
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	opened, openErr := next.Stat(".")
	after, afterErr := root.Stat(".")
	if openErr != nil || afterErr != nil || !sameIdentity(before, opened) || !sameIdentity(before, after) || ownerOnly(next) != nil {
		_ = next.Close()
		return nil, ErrUnsafeRoot
	}
	return next, nil
}

// Close releases this duplicate capability. It is safe to call more than once.
func (directory *StateDirectory) Close() error {
	if directory == nil || directory.root == nil {
		return nil
	}
	root := directory.root
	directory.root = nil
	return root.Close()
}

// CreateDirectory creates one new owner-only child directory.
func (directory *StateDirectory) CreateDirectory(name string) error {
	if !directory.validName(name) {
		return directoryError(directory)
	}
	if _, err := directory.root.Lstat(name); err == nil || !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	if err := directory.root.Mkdir(name, 0o700); err != nil {
		return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
	}
	child, err := directory.OpenDirectory(name)
	if err != nil {
		return err
	}
	return child.Close()
}

// OpenDirectory opens one existing owner-only child without following links.
func (directory *StateDirectory) OpenDirectory(name string) (*StateDirectory, error) {
	if !directory.validName(name) {
		return nil, directoryError(directory)
	}
	before, err := directory.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrStateEntryNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.IsDir() {
		return nil, ErrUnsafeRoot
	}
	next, err := directory.root.OpenRoot(name)
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	opened, openErr := next.Stat(".")
	after, afterErr := directory.root.Lstat(name)
	if openErr != nil || afterErr != nil || after.Mode()&os.ModeSymlink != 0 || !sameIdentity(before, opened) || !sameIdentity(before, after) || !sameIdentity(opened, after) || ownerOnly(next) != nil || directory.roots.rejectProtectedDirectory(opened) != nil {
		_ = next.Close()
		return nil, ErrUnsafeRoot
	}
	return &StateDirectory{root: next, roots: directory.roots}, nil
}

// CreateFile creates one new owner-only regular file without truncation.
func (directory *StateDirectory) CreateFile(name string) (*os.File, error) {
	if !directory.validName(name) {
		return nil, directoryError(directory)
	}
	if _, err := directory.root.Lstat(name); err == nil || !errors.Is(err, os.ErrNotExist) {
		return nil, ErrUnsafeRoot
	}
	file, err := directory.root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	if safeStateFile(file) != nil {
		_ = file.Close()
		return nil, ErrUnsafeRoot
	}
	return file, nil
}

// OpenFile opens and validates one existing owner-only regular file.
func (directory *StateDirectory) OpenFile(name string) (*os.File, error) {
	if !directory.validName(name) {
		return nil, directoryError(directory)
	}
	before, err := directory.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrStateEntryNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() {
		return nil, ErrUnsafeRoot
	}
	file, err := directory.root.Open(name)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, ErrStateEntryChanged
		}
		return nil, ErrUnsafeRoot
	}
	opened, openErr := file.Stat()
	after, afterErr := directory.root.Lstat(name)
	if openErr != nil {
		_ = file.Close()
		return nil, ErrUnsafeRoot
	}
	if errors.Is(afterErr, os.ErrNotExist) {
		_ = file.Close()
		return nil, ErrStateEntryChanged
	}
	if afterErr != nil || after.Mode()&os.ModeSymlink != 0 || !after.Mode().IsRegular() {
		_ = file.Close()
		return nil, ErrUnsafeRoot
	}
	if !sameSnapshot(before, opened) || !sameSnapshot(before, after) {
		_ = file.Close()
		return nil, ErrStateEntryChanged
	}
	if safeStateFile(file) != nil {
		latest, latestErr := directory.root.Lstat(name)
		_ = file.Close()
		if errors.Is(latestErr, os.ErrNotExist) || (latestErr == nil && latest.Mode()&os.ModeSymlink == 0 && latest.Mode().IsRegular() && (!sameSnapshot(before, latest) || errors.Is(safeLiveStateEntry(latest), ErrStateEntryChanged))) {
			return nil, ErrStateEntryChanged
		}
		return nil, ErrUnsafeRoot
	}
	return file, nil
}

// ReadFile performs one bounded stable read and proves the directory entry
// still names the opened owner-only regular file after the read.
func (directory *StateDirectory) ReadFile(name string, maximum int64) ([]byte, error) {
	if !directory.validName(name) || maximum < 0 {
		return nil, directoryError(directory)
	}
	before, err := directory.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrStateEntryNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() || before.Size() > maximum {
		return nil, ErrUnsafeRoot
	}
	file, err := directory.OpenFile(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	contents, err := readAtMost(file, maximum, nil)
	if err != nil {
		return nil, ErrUnsafeRoot
	}
	opened, openErr := file.Stat()
	after, afterErr := directory.root.Lstat(name)
	if openErr != nil {
		return nil, ErrUnsafeRoot
	}
	if errors.Is(afterErr, os.ErrNotExist) {
		return nil, ErrStateEntryChanged
	}
	if afterErr != nil || after.Mode()&os.ModeSymlink != 0 || !after.Mode().IsRegular() {
		return nil, ErrUnsafeRoot
	}
	if !sameSnapshot(before, opened) || !sameSnapshot(before, after) {
		return nil, ErrStateEntryChanged
	}
	return contents, nil
}

// ReadAtomicCurrent reads the sole mutable state pointer, which trusted code
// replaces only by atomic rename. os.Root.Open rejects a terminal symlink, and
// the opened descriptor must remain an owner-only, non-hardlinked, exact-size
// regular file throughout the read. It may be any complete prior or next inode
// opened during churn. Immutable generation files must continue to use ReadFile.
func (directory *StateDirectory) ReadAtomicCurrent(exactLength int64) ([]byte, error) {
	const name = "CURRENT"
	if exactLength < 0 {
		return nil, directoryError(directory)
	}
	before, err := directory.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrStateEntryNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 || !before.Mode().IsRegular() || before.Size() != exactLength {
		return nil, fmt.Errorf("%w: atomic pointer pre-open", ErrUnsafeRoot)
	}
	file, err := directory.root.Open(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrStateEntryChanged
	}
	if err != nil {
		return nil, fmt.Errorf("%w: atomic pointer open", ErrUnsafeRoot)
	}
	defer file.Close()
	openedBefore, err := file.Stat()
	if err != nil || safeAtomicStateFile(file) != nil || openedBefore.Size() != exactLength {
		return nil, fmt.Errorf("%w: atomic pointer opened handle", ErrUnsafeRoot)
	}
	contents, err := readAtMost(file, exactLength, nil)
	if err != nil || int64(len(contents)) != exactLength {
		return nil, fmt.Errorf("%w: atomic pointer read", ErrUnsafeRoot)
	}
	openedAfter, openErr := file.Stat()
	if openErr != nil || safeAtomicStateFile(file) != nil || !sameSnapshot(openedBefore, openedAfter) {
		return nil, fmt.Errorf("%w: atomic pointer handle changed", ErrUnsafeRoot)
	}
	return contents, nil
}

// RenameNew atomically renames one validated file or directory to a name that
// must not already exist.
func (directory *StateDirectory) RenameNew(source, destination string) error {
	if !directory.validName(source) || !directory.validName(destination) {
		return directoryError(directory)
	}
	if _, err := directory.root.Lstat(destination); err == nil || !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	retained, before, err := directory.retainEntry(source, false)
	if err != nil {
		return err
	}
	defer retained.Close()
	if err := directory.root.Rename(source, destination); err != nil {
		return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
	}
	after, err := directory.root.Lstat(destination)
	if err != nil || after.Mode()&os.ModeSymlink != 0 || !sameIdentity(before, after) {
		return ErrUnsafeRoot
	}
	return nil
}

// ReplaceFile atomically renames one validated regular file over a validated
// regular destination, or to an absent destination.
func (directory *StateDirectory) ReplaceFile(source, destination string) error {
	if !directory.validName(source) || !directory.validName(destination) {
		return directoryError(directory)
	}
	retained, before, err := directory.retainEntry(source, true)
	if err != nil {
		return err
	}
	defer retained.Close()
	if existing, err := directory.root.Lstat(destination); err == nil {
		if existing.Mode()&os.ModeSymlink != 0 || !existing.Mode().IsRegular() {
			return ErrUnsafeRoot
		}
		file, openErr := directory.OpenFile(destination)
		if openErr != nil {
			return openErr
		}
		defer file.Close()
	} else if !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	if err := directory.root.Rename(source, destination); err != nil {
		return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
	}
	after, err := directory.root.Lstat(destination)
	if err != nil || after.Mode()&os.ModeSymlink != 0 || !after.Mode().IsRegular() || !sameIdentity(before, after) {
		return ErrUnsafeRoot
	}
	return nil
}

func (directory *StateDirectory) retainEntry(name string, requireFile bool) (*os.File, os.FileInfo, error) {
	before, err := directory.root.Lstat(name)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil, ErrStateEntryNotFound
	}
	if err != nil || before.Mode()&os.ModeSymlink != 0 {
		return nil, nil, ErrUnsafeRoot
	}
	if requireFile || before.Mode().IsRegular() {
		if !before.Mode().IsRegular() {
			return nil, nil, ErrUnsafeRoot
		}
		file, err := directory.OpenFile(name)
		return file, before, err
	}
	if !before.IsDir() {
		return nil, nil, ErrUnsafeRoot
	}
	root, err := directory.OpenDirectory(name)
	if err != nil {
		return nil, nil, err
	}
	file, err := root.root.Open(".")
	_ = root.Close()
	if err != nil {
		return nil, nil, ErrUnsafeRoot
	}
	if ownerOnlyOpenFile(file) != nil {
		_ = file.Close()
		return nil, nil, ErrUnsafeRoot
	}
	return file, before, nil
}

// Sync durably flushes this retained directory.
func (directory *StateDirectory) Sync() error {
	if directory == nil || directory.root == nil {
		return ErrStateUnavailable
	}
	file, err := directory.root.Open(".")
	if err != nil {
		return ErrUnsafeRoot
	}
	if ownerOnlyOpenFile(file) != nil {
		_ = file.Close()
		return ErrUnsafeRoot
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("%w: %v", ErrUnsafeRoot, err)
	}
	return nil
}

// Names returns at most maximum validated immediate entry names in byte order.
func (directory *StateDirectory) Names(maximum int) ([]string, error) {
	if directory == nil || directory.root == nil {
		return nil, ErrStateUnavailable
	}
	if maximum < 0 || maximum > 1024 {
		return nil, ErrUnsafeRoot
	}
	file, err := directory.root.Open(".")
	if err != nil || ownerOnlyOpenFile(file) != nil {
		if file != nil {
			_ = file.Close()
		}
		return nil, ErrUnsafeRoot
	}
	entries, readErr := file.ReadDir(maximum + 1)
	closeErr := file.Close()
	if readErr != nil && !errors.Is(readErr, io.EOF) {
		return nil, ErrUnsafeRoot
	}
	if closeErr != nil || len(entries) > maximum {
		return nil, ErrUnsafeRoot
	}
	names := make([]string, len(entries))
	for index, entry := range entries {
		name := entry.Name()
		components, err := safeComponents(name, false)
		if err != nil || len(components) != 1 {
			return nil, ErrUnsafeRoot
		}
		names[index] = name
	}
	sort.Strings(names)
	return names, nil
}

// RemoveFile removes one validated owner-only regular file.
func (directory *StateDirectory) RemoveFile(name string) error {
	if !directory.validName(name) {
		return directoryError(directory)
	}
	file, _, err := directory.retainEntry(name, true)
	if errors.Is(err, ErrStateEntryNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	defer file.Close()
	if err := directory.root.Remove(name); err != nil {
		return ErrUnsafeRoot
	}
	if _, err := directory.root.Lstat(name); !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	return nil
}

// RemoveEmptyDirectory removes one validated owner-only empty directory.
func (directory *StateDirectory) RemoveEmptyDirectory(name string) error {
	if !directory.validName(name) {
		return directoryError(directory)
	}
	child, err := directory.OpenDirectory(name)
	if errors.Is(err, ErrStateEntryNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	defer child.Close()
	entries, err := child.Names(0)
	if err != nil || len(entries) != 0 {
		return ErrUnsafeRoot
	}
	if err := directory.root.Remove(name); err != nil {
		return ErrUnsafeRoot
	}
	if _, err := directory.root.Lstat(name); !errors.Is(err, os.ErrNotExist) {
		return ErrUnsafeRoot
	}
	return nil
}

func (directory *StateDirectory) validName(name string) bool {
	if directory == nil || directory.root == nil || name == "" {
		return false
	}
	components, err := safeComponents(name, false)
	return err == nil && len(components) == 1
}

func directoryError(directory *StateDirectory) error {
	if directory == nil || directory.root == nil {
		return ErrStateUnavailable
	}
	return ErrUnsafeRoot
}
