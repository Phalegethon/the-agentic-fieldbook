package policy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestLimitsMatchFrozenProductionPolicy(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "policy", "production-v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var artifact Limits
	if err := json.Unmarshal(raw, &artifact); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(artifact, ProductionV1) {
		t.Fatalf("policy artifact and runtime differ: artifact=%+v runtime=%+v", artifact, ProductionV1)
	}
}
