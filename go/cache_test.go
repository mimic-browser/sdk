package mimic

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPruneVerifiesTargetAndLiveLeases(t *testing.T) {
	root := t.TempDir()
	manager := NewRuntimeManager(RuntimeOptions{RuntimeDir: root})
	bytes := []byte("verified fixture executable")
	installation := Installation{Release: "v0.2.2", Platform: "linux-amd64", SourceRevision: strings.Repeat("a", 40), ArchiveSHA256: strings.Repeat("b", 64), BinarySHA256: checksum(bytes), ManifestSHA256: strings.Repeat("c", 64), Executable: "mimic"}
	directory := filepath.Join(root, installation.Release, installation.Platform, installation.BinarySHA256)
	if err := os.MkdirAll(filepath.Join(directory, ".leases"), 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(installation)
	if err := os.WriteFile(filepath.Join(directory, "installation.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	installation.Path = filepath.Join(directory, "mimic")
	if err := os.WriteFile(installation.Path, bytes, 0700); err != nil {
		t.Fatal(err)
	}
	listed, err := manager.List()
	if err != nil || len(listed) != 1 || listed[0] != installation {
		t.Fatalf("list: %#v %v", listed, err)
	}
	hostname, _ := os.Hostname()
	raw, _ = json.Marshal(map[string]any{"runtimePid": os.Getpid(), "hostname": hostname})
	lease := filepath.Join(directory, ".leases", "fixture.json")
	if err = os.WriteFile(lease, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err = manager.Prune(context.Background(), installation); err == nil || !strings.Contains(err.Error(), "running") {
		t.Fatalf("live lease: %v", err)
	}
	if err = os.WriteFile(lease, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err = manager.Prune(context.Background(), installation); err == nil {
		t.Fatal("incomplete lease accepted")
	}
	if err = os.Remove(lease); err != nil {
		t.Fatal(err)
	}
	escaped := installation
	escaped.Path = filepath.Join(root, "outside", "mimic")
	if err = manager.Prune(context.Background(), escaped); err == nil {
		t.Fatal("escaped prune accepted")
	}
	if err = manager.Prune(context.Background(), installation); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(directory); !os.IsNotExist(err) {
		t.Fatalf("installation retained: %v", err)
	}
}

func TestExplicitLockRejectsBinaryBeforeStarting(t *testing.T) {
	if _, err := hostPlatform(); err != nil {
		t.Skip(err)
	}
	dir := t.TempDir()
	lock := filepath.Join(dir, "lock.json")
	if err := os.WriteFile(lock, defaultLockJSON, 0600); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(dir, "not-the-pinned-runtime")
	if err := os.WriteFile(bin, []byte("never execute this"), 0700); err != nil {
		t.Fatal(err)
	}
	_, err := NewRuntimeManager(RuntimeOptions{ExecutablePath: bin, LockFile: lock}).Launch(context.Background())
	if err == nil || !strings.Contains(err.Error(), "lock hash") {
		t.Fatalf("not rejected before launch: %v", err)
	}
}
