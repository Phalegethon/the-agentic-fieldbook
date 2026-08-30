package store

import (
	"os"
	"os/exec"
	"testing"
)

func makeInsecurePermissions(t *testing.T, path string, _ os.FileMode) {
	t.Helper()
	output, err := exec.Command("icacls", path, "/grant", "*S-1-1-0:(RX)").CombinedOutput()
	if err != nil {
		t.Fatalf("make permissions insecure: %v\n%s", err, output)
	}
}
