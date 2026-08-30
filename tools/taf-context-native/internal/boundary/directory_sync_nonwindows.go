//go:build !windows

package boundary

import "os"

func syncDirectory(file *os.File) error {
	return file.Sync()
}
