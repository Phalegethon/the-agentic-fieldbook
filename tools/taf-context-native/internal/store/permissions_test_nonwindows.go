//go:build !windows

package store

import (
	"os"
	"testing"
)

func makeInsecurePermissions(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}
