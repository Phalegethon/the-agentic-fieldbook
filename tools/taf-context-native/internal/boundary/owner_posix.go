//go:build !windows

package boundary

import (
	"fmt"
	"os"
	"syscall"
)

func ownerOnly(root *os.Root) error {
	info, err := root.Stat(".")
	if err != nil {
		return ErrUnsafeRoot
	}
	return ownerOnlyFile(info)
}

func ownerOnlyFile(info os.FileInfo) error {
	if info.Mode().Perm()&0o077 != 0 {
		return ErrUnsafeRoot
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Geteuid() {
		return ErrUnsafeRoot
	}
	return nil
}

func ownerOnlyPath(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return ErrUnsafeRoot
	}
	return ownerOnlyFile(info)
}

func ownerOnlyOpenFile(file *os.File) error {
	info, err := file.Stat()
	if err != nil {
		return ErrUnsafeRoot
	}
	return ownerOnlyFile(info)
}

func safeStateFile(file *os.File) error {
	info, err := file.Stat()
	if err != nil {
		return ErrUnsafeRoot
	}
	if err := ownerOnlyFile(info); err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Nlink != 1 {
		return ErrUnsafeRoot
	}
	return nil
}

func safeAtomicStateFile(file *os.File) error {
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return ErrUnsafeRoot
	}
	if err := ownerOnlyFile(info); err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Nlink > 1 {
		return ErrUnsafeRoot
	}
	return nil
}

func safeLiveStateEntry(info os.FileInfo) error {
	if info == nil || !info.Mode().IsRegular() {
		return fmt.Errorf("%w: not regular", ErrUnsafeRoot)
	}
	if ownerOnlyFile(info) != nil {
		return fmt.Errorf("%w: owner or mode", ErrUnsafeRoot)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("%w: missing stat", ErrUnsafeRoot)
	}
	if stat.Nlink == 0 {
		return ErrStateEntryChanged
	}
	if stat.Nlink > 1 {
		return fmt.Errorf("%w: link count %d", ErrUnsafeRoot, stat.Nlink)
	}
	return nil
}
