package mimic

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Verify checks a receipt and the executable bytes without downloading or
// replacing anything. Install additionally checks the selected lock provenance.
func (m *RuntimeManager) Verify(directory string) (*Installation, error) {
	raw, err := os.ReadFile(filepath.Join(directory, "installation.json"))
	if err != nil {
		return nil, err
	}
	var installation Installation
	if err = json.Unmarshal(raw, &installation); err != nil {
		return nil, err
	}
	v, err := normalizeVersion(installation.Release)
	if err != nil || v != installation.Release || !hashPattern.MatchString(installation.BinarySHA256) || !hashPattern.MatchString(installation.ArchiveSHA256) || !hashPattern.MatchString(installation.ManifestSHA256) || !revisionPattern.MatchString(installation.SourceRevision) {
		return nil, errors.New("invalid installation receipt identity")
	}
	name := "mimic"
	if installation.Platform == "windows-amd64" {
		name = "mimic.exe"
	} else if installation.Platform != "linux-amd64" {
		return nil, errors.New("invalid installation platform")
	}
	if installation.Executable != name {
		return nil, errors.New("invalid installation executable path")
	}
	if err = verifyInstallation(directory, installation); err != nil {
		return nil, err
	}
	installation.Path = filepath.Join(directory, name)
	return &installation, nil
}

func (m *RuntimeManager) List() ([]Installation, error) {
	root, err := m.root()
	if err != nil {
		return nil, err
	}
	files, err := filepath.Glob(filepath.Join(root, "v*", "*-amd64", "*", "installation.json"))
	if err != nil {
		return nil, err
	}
	result := make([]Installation, 0, len(files))
	for _, file := range files {
		item, err := m.Verify(filepath.Dir(file))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", file, err)
		}
		result = append(result, *item)
	}
	return result, nil
}

// Prune removes one verified unused installation. A live or unverifiable lease
// blocks removal; other installed versions and user browser data are untouched.
func (m *RuntimeManager) Prune(ctx context.Context, installation Installation) error {
	root, err := m.root()
	if err != nil {
		return err
	}
	directory, err := filepath.Abs(filepath.Dir(installation.Path))
	if err != nil {
		return err
	}
	expected := filepath.Join(root, installation.Release, installation.Platform, installation.BinarySHA256)
	if directory != expected {
		return errors.New("prune target is outside the selected installation root")
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	resolved, err := filepath.EvalSymlinks(directory)
	if err != nil {
		return err
	}
	if resolved != filepath.Join(resolvedRoot, installation.Release, installation.Platform, installation.BinarySHA256) {
		return errors.New("prune target traverses a symbolic link")
	}
	unlock, err := m.acquire(ctx, root, installation.Release+"-"+installation.Platform)
	if err != nil {
		return err
	}
	defer unlock()
	if _, err = m.Verify(directory); err != nil {
		return err
	}
	if err = verifyInstallation(directory, installation); err != nil {
		return err
	}
	leases, err := filepath.Glob(filepath.Join(directory, ".leases", "*.json"))
	if err != nil {
		return err
	}
	hostname, err := os.Hostname()
	if err != nil {
		return err
	}
	for _, file := range leases {
		raw, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		var lease struct {
			Hostname   string `json:"hostname"`
			RuntimePID int    `json:"runtimePid"`
		}
		if json.Unmarshal(raw, &lease) != nil || lease.Hostname != hostname || lease.RuntimePID <= 0 {
			return errors.New("foreign or incomplete runtime lease blocks pruning")
		}
		if processAlive(lease.RuntimePID) {
			return errors.New("cannot prune an installation with a running runtime lease")
		}
	}
	return os.RemoveAll(directory)
}
