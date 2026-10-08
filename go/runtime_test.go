package mimic

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRuntimeSelectorsAndLockIntegrity(t *testing.T) {
	t.Setenv("MIMIC_RUNTIME_VERSION", "")
	manager := NewRuntimeManager(RuntimeOptions{})
	lock, err := manager.ResolveLock(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var bundled RuntimeLock
	if err := json.Unmarshal(defaultLockJSON, &bundled); err != nil {
		t.Fatal(err)
	}
	if lock.Release != bundled.Release {
		t.Fatal(lock.Release)
	}
	dir := t.TempDir()
	file := filepath.Join(dir, "lock.json")
	data, _ := json.Marshal(lock)
	if err = os.WriteFile(file, data, 0600); err != nil {
		t.Fatal(err)
	}
	manager.Options = RuntimeOptions{Version: "0.2.1", LockFile: file}
	if _, err = manager.ResolveLock(context.Background()); err == nil || !strings.Contains(err.Error(), "conflict") {
		t.Fatalf("conflict accepted: %v", err)
	}
	for _, version := range []string{"latest", "../v0.2.2", "0.2", "0.2.2+x", " v0.2.2"} {
		if _, err = normalizeVersion(version); err == nil {
			t.Fatalf("accepted %q", version)
		}
	}
	lock.ManifestJSON += " "
	if _, err = lock.Validate(); err == nil {
		t.Fatal("accepted replaced manifest bytes")
	}
	lock.ManifestSHA256 = checksum([]byte(lock.ManifestJSON))
	if _, err = lock.Validate(); err != nil {
		t.Fatal(err)
	}
	lock.Manifest = []byte(`{}`)
	if _, err = lock.Validate(); err == nil {
		t.Fatal("accepted inconsistent parsed manifest")
	}
}

func TestImmutableManifestAndLockOwnership(t *testing.T) {
	root := t.TempDir()
	filename := filepath.Join(root, "manifest.json")
	if err := publishManifest(filename, defaultLockJSON); err != nil {
		t.Fatal(err)
	}
	if err := publishManifest(filename, defaultLockJSON); err != nil {
		t.Fatal(err)
	}
	var replaced RuntimeLock
	if err := json.Unmarshal(defaultLockJSON, &replaced); err != nil {
		t.Fatal(err)
	}
	replaced.ManifestJSON += " "
	replaced.ManifestSHA256 = checksum([]byte(replaced.ManifestJSON))
	raw, _ := json.Marshal(replaced)
	if err := publishManifest(filename, raw); err == nil {
		t.Fatal("immutable manifest replacement accepted")
	}
	manager := NewRuntimeManager(RuntimeOptions{})
	unlock, err := manager.acquire(context.Background(), root, "owner-test")
	if err != nil {
		t.Fatal(err)
	}
	owner := filepath.Join(root, ".locks", "owner-test.lock", "owner.json")
	if err = os.WriteFile(owner, []byte(`{"token":"replacement-owner"}`), 0600); err != nil {
		t.Fatal(err)
	}
	unlock()
	if _, err = os.Stat(owner); err != nil {
		t.Fatal("released another owner's lock")
	}
}

func TestArchiveRejectsTraversalLinksAndDuplicates(t *testing.T) {
	for _, test := range []struct {
		name      string
		mode      os.FileMode
		duplicate bool
	}{
		{"root/../escape", 0600, false}, {"/root/escape", 0600, false}, {"root/dir\\escape", 0600, false}, {"other/file", 0600, false}, {"root/link", os.ModeSymlink | 0600, false}, {"root/file", 0600, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var buf bytes.Buffer
			z := zip.NewWriter(&buf)
			h := &zip.FileHeader{Name: test.name}
			h.SetMode(test.mode)
			w, err := z.CreateHeader(h)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = w.Write([]byte("x"))
			if test.duplicate {
				copy := *h
				w, err = z.CreateHeader(&copy)
				if err != nil {
					t.Fatal(err)
				}
				_, _ = w.Write([]byte("x"))
			}
			_ = z.Close()
			if err = extractArchive(buf.Bytes(), "fixture.zip", filepath.Join(t.TempDir(), "tree"), "root"); err == nil {
				t.Fatal("unsafe archive accepted")
			}
		})
	}
}

func TestInstallLockExclusionAndCancellation(t *testing.T) {
	manager := NewRuntimeManager(RuntimeOptions{LockTimeout: 50 * time.Millisecond})
	root := t.TempDir()
	unlock, err := manager.acquire(context.Background(), root, "v0.2.2-linux-amd64")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = manager.acquire(context.Background(), root, "v0.2.2-linux-amd64"); err == nil {
		t.Fatal("competing install acquired active lock")
	}
	unlock()
	unlock, err = manager.acquire(context.Background(), root, "v0.2.2-linux-amd64")
	if err != nil {
		t.Fatal(err)
	}
	unlock()
}

func TestOfficialOfflineInstallationAndTamper(t *testing.T) {
	archive := os.Getenv("MIMIC_SDK_TEST_ARCHIVE")
	if archive == "" {
		t.Skip("set MIMIC_SDK_TEST_ARCHIVE to an exact official release archive")
	}
	allow := false
	manager := NewRuntimeManager(RuntimeOptions{RuntimeDir: t.TempDir(), ArchivePath: archive, AllowDownload: &allow})
	first, err := manager.Install(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	manager.Options.ArchivePath = ""
	second, err := manager.Install(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if first.Path != second.Path {
		t.Fatal("offline cache was not reused")
	}
	f, err := os.OpenFile(first.Path, os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteAt([]byte("corruption"), 0)
	_ = f.Close()
	if _, err = manager.Install(context.Background()); err == nil || !strings.Contains(err.Error(), "hash") {
		t.Fatalf("accepted corrupt binary: %v", err)
	}
}
