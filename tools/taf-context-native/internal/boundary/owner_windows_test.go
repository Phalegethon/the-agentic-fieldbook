//go:build windows

package boundary

import (
	"os/exec"
	"testing"
)

func TestOwnerOnlyAcceptsProcessPrivateTemp(t *testing.T) {
	path := t.TempDir()
	if err := ownerOnlyPath(path); err != nil {
		output, inspectErr := exec.Command("icacls", path).CombinedOutput()
		t.Fatalf("owner-only temp rejected: %v; icacls error: %v\n%s", err, inspectErr, output)
	}
}
