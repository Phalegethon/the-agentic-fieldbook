package boundary

import "os"

// Windows does not support flushing an opened directory with FlushFileBuffers.
// Regular state files are still synced before atomic rename; the directory
// capability and its ACL are validated by StateDirectory.Sync before this call.
func syncDirectory(_ *os.File) error {
	return nil
}
