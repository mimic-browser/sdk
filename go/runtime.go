package mimic

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/rand"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed runtime-lock.json
var defaultLockJSON []byte

type Artifact struct {
	Platform      string `json:"platform"`
	Archive       string `json:"archive"`
	SHA256        string `json:"sha256"`
	Size          int64  `json:"size"`
	BinarySHA256  string `json:"binarySha256"`
	BinaryVersion string `json:"binaryVersion"`
}
type Manifest struct {
	Version        string     `json:"version"`
	SourceRevision string     `json:"sourceRevision"`
	Artifacts      []Artifact `json:"artifacts"`
}
type RuntimeLock struct {
	Release        string          `json:"release"`
	ManifestSHA256 string          `json:"manifestSha256"`
	ManifestJSON   string          `json:"manifestJson"`
	Manifest       json.RawMessage `json:"manifest"`
	BaseURL        string          `json:"baseUrl"`
}
type RuntimeOptions struct {
	Version        string
	LockFile       string
	ExecutablePath string
	RuntimeDir     string
	AllowDownload  *bool
	StartupTimeout time.Duration
	LockTimeout    time.Duration
	// ArchivePath is an explicit, hash-verified offline installation source.
	ArchivePath string
}
type Installation struct {
	Release        string `json:"release"`
	Platform       string `json:"platform"`
	SourceRevision string `json:"sourceRevision"`
	ArchiveSHA256  string `json:"archiveSha256"`
	BinarySHA256   string `json:"binarySha256"`
	Executable     string `json:"executable"`
	ManifestSHA256 string `json:"manifestSha256"`
	Path           string `json:"-"`
}
type RuntimeManager struct {
	Options    RuntimeOptions
	HTTPClient *http.Client
}

func NewRuntimeManager(options RuntimeOptions) *RuntimeManager {
	return &RuntimeManager{Options: options, HTTPClient: &http.Client{Timeout: 2 * time.Minute}}
}

var exactVersion = regexp.MustCompile(`^v?\d+\.\d+\.\d+(?:-beta\.\d+)?$`)
var hashPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var revisionPattern = regexp.MustCompile(`^[a-f0-9]{40}$`)

func normalizeVersion(v string) (string, error) {
	if !exactVersion.MatchString(v) {
		return "", fmt.Errorf("expected an exact runtime version, got %q", v)
	}
	return "v" + strings.TrimPrefix(v, "v"), nil
}
func checksum(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func fileDigest(p string) (string, error) {
	f, e := os.Open(p)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func token() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func (l RuntimeLock) Validate() (Manifest, error) {
	var m Manifest
	v, err := normalizeVersion(l.Release)
	if err != nil || v != l.Release {
		return m, errors.New("invalid normalized lock release")
	}
	if !hashPattern.MatchString(l.ManifestSHA256) || checksum([]byte(l.ManifestJSON)) != l.ManifestSHA256 {
		return m, errors.New("manifest digest mismatch")
	}
	var a, b any
	if json.Unmarshal([]byte(l.ManifestJSON), &a) != nil || json.Unmarshal(l.Manifest, &b) != nil || !reflect.DeepEqual(a, b) {
		return m, errors.New("lock manifest differs from retained bytes")
	}
	if err = json.Unmarshal([]byte(l.ManifestJSON), &m); err != nil {
		return m, err
	}
	if m.Version != l.Release || !revisionPattern.MatchString(m.SourceRevision) {
		return m, errors.New("manifest release/source identity mismatch")
	}
	if l.BaseURL != "https://github.com/mimic-browser/runtime/releases/download/"+l.Release {
		return m, errors.New("runtime lock must reference its exact official release")
	}
	seen := map[string]bool{}
	for _, a := range m.Artifacts {
		ext := ".tar.gz"
		if a.Platform == "windows-amd64" {
			ext = ".zip"
		} else if a.Platform != "linux-amd64" {
			return m, errors.New("unsupported manifest platform")
		}
		if seen[a.Platform] || a.Archive != "mimic-"+l.Release+"-"+a.Platform+ext || !hashPattern.MatchString(a.SHA256) || !hashPattern.MatchString(a.BinarySHA256) || a.Size <= 0 || a.Size > 1<<30 || a.BinaryVersion != l.Release {
			return m, errors.New("invalid artifact identity")
		}
		seen[a.Platform] = true
	}
	if len(seen) == 0 {
		return m, errors.New("empty runtime manifest")
	}
	return m, nil
}

func (m *RuntimeManager) root() (string, error) {
	root := m.Options.RuntimeDir
	if root == "" {
		root = os.Getenv("MIMIC_RUNTIME_DIR")
	}
	if root == "" {
		if runtime.GOOS == "windows" {
			base := os.Getenv("LOCALAPPDATA")
			if base == "" {
				return "", errors.New("LOCALAPPDATA is missing; set MIMIC_RUNTIME_DIR")
			}
			root = filepath.Join(base, "Mimic", "runtimes")
		} else {
			base := os.Getenv("XDG_CACHE_HOME")
			if base == "" {
				home, err := os.UserHomeDir()
				if err != nil {
					return "", err
				}
				base = filepath.Join(home, ".cache")
			}
			root = filepath.Join(base, "Mimic", "runtimes")
		}
	}
	return filepath.Abs(root)
}
func hostPlatform() (string, error) {
	if runtime.GOARCH != "amd64" || (runtime.GOOS != "windows" && runtime.GOOS != "linux") {
		return "", fmt.Errorf("unsupported runtime host %s-%s", runtime.GOOS, runtime.GOARCH)
	}
	if runtime.GOOS == "linux" {
		out, err := exec.Command("getconf", "GNU_LIBC_VERSION").Output()
		if err != nil {
			return "", errors.New("Linux runtime requires glibc 2.39+")
		}
		var major, minor int
		if _, err = fmt.Sscanf(strings.TrimSpace(string(out)), "glibc %d.%d", &major, &minor); err != nil || major < 2 || (major == 2 && minor < 39) {
			return "", errors.New("Linux runtime requires glibc 2.39+")
		}
	}
	return runtime.GOOS + "-amd64", nil
}
func (m *RuntimeManager) downloads() bool {
	if m.Options.AllowDownload != nil {
		return *m.Options.AllowDownload
	}
	return os.Getenv("MIMIC_DOWNLOAD") != "0"
}
func (m *RuntimeManager) get(ctx context.Context, url string, limit int64) ([]byte, error) {
	if !m.downloads() {
		return nil, errors.New("downloads disabled; preinstall the exact runtime or supply a lock and archive")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	client := m.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 2 * time.Minute}
	}
	r, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return nil, fmt.Errorf("runtime download HTTP %d for %s", r.StatusCode, url)
	}
	b, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errors.New("runtime download exceeds size limit")
	}
	return b, nil
}
func (m *RuntimeManager) ResolveLock(ctx context.Context) (RuntimeLock, error) {
	var l RuntimeLock
	var err error
	if m.Options.LockFile != "" {
		b, e := os.ReadFile(m.Options.LockFile)
		if e != nil {
			return l, e
		}
		if err = json.Unmarshal(b, &l); err != nil {
			return l, err
		}
	}
	v := m.Options.Version
	if v != "" {
		v, err = normalizeVersion(v)
		if err != nil {
			return l, err
		}
		if l.Release != "" && v != l.Release {
			return l, errors.New("explicit runtime version and lock conflict")
		}
	}
	if l.Release != "" {
		_, err = l.Validate()
		return l, err
	}
	if v == "" {
		v = os.Getenv("MIMIC_RUNTIME_VERSION")
		if v != "" {
			v, err = normalizeVersion(v)
			if err != nil {
				return l, err
			}
		}
	}
	if err = json.Unmarshal(defaultLockJSON, &l); err != nil {
		return l, err
	}
	if v == "" || v == l.Release {
		_, err = l.Validate()
		return l, err
	}
	root, err := m.root()
	if err != nil {
		return l, err
	}
	cached := filepath.Join(root, ".manifests", v+".json")
	if data, e := os.ReadFile(cached); e == nil {
		if e = json.Unmarshal(data, &l); e != nil {
			return l, e
		}
		if l.Release != v {
			return l, errors.New("cached manifest version mismatch")
		}
		_, e = l.Validate()
		return l, e
	} else if !os.IsNotExist(e) {
		return l, e
	}
	base := "https://github.com/mimic-browser/runtime/releases/download/" + v
	manifest, err := m.get(ctx, base+"/release-manifest.json", 4<<20)
	if err != nil {
		return l, err
	}
	sums, err := m.get(ctx, base+"/SHA256SUMS", 1<<20)
	if err != nil {
		return l, err
	}
	digest := checksum(manifest)
	matched := false
	for _, line := range strings.Split(string(sums), "\n") {
		if strings.TrimSpace(line) == digest+"  release-manifest.json" {
			matched = true
		}
	}
	if !matched {
		return l, errors.New("release manifest SHA256SUMS mismatch")
	}
	l = RuntimeLock{Release: v, ManifestSHA256: digest, ManifestJSON: string(manifest), Manifest: manifest, BaseURL: base}
	if _, err = l.Validate(); err != nil {
		return l, err
	}
	if err = os.MkdirAll(filepath.Dir(cached), 0700); err != nil {
		return l, err
	}
	raw, _ := json.Marshal(l)
	if err = publishManifest(cached, raw); err != nil {
		return l, err
	}
	return l, nil
}
func atomicJSON(filename string, raw []byte) error {
	tmp := filename + "." + token() + ".tmp"
	if err := os.WriteFile(tmp, raw, 0600); err != nil {
		return err
	}
	defer os.Remove(tmp)
	return os.Rename(tmp, filename)
}

// Publish fully written bytes without ever replacing another language's
// immutable release manifest. Hard-link publication is atomic on supported
// Linux/NTFS cache filesystems; unsupported filesystems fail explicitly.
func publishManifest(filename string, raw []byte) error {
	tmp := filename + "." + token() + ".tmp"
	if err := os.WriteFile(tmp, raw, 0600); err != nil {
		return err
	}
	defer os.Remove(tmp)
	if err := os.Link(tmp, filename); err == nil {
		return nil
	} else if !os.IsExist(err) {
		return fmt.Errorf("atomic immutable manifest publication: %w", err)
	}
	existing, err := os.ReadFile(filename)
	if err != nil {
		return err
	}
	var old, next RuntimeLock
	if json.Unmarshal(existing, &old) != nil || json.Unmarshal(raw, &next) != nil {
		return errors.New("invalid immutable manifest")
	}
	if _, err = old.Validate(); err != nil {
		return err
	}
	if _, err = next.Validate(); err != nil {
		return err
	}
	if old.Release != next.Release || old.ManifestSHA256 != next.ManifestSHA256 {
		return errors.New("conflicting immutable release manifest")
	}
	return nil
}

func (m *RuntimeManager) Install(ctx context.Context) (*Installation, error) {
	platform, err := hostPlatform()
	if err != nil {
		return nil, err
	}
	l, err := m.ResolveLock(ctx)
	if err != nil {
		return nil, err
	}
	manifest, err := l.Validate()
	if err != nil {
		return nil, err
	}
	var artifact Artifact
	for _, a := range manifest.Artifacts {
		if a.Platform == platform {
			artifact = a
		}
	}
	if artifact.Archive == "" {
		return nil, fmt.Errorf("release %s has no %s artifact", l.Release, platform)
	}
	name := "mimic"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	receipt := &Installation{Release: l.Release, Platform: platform, SourceRevision: manifest.SourceRevision, ArchiveSHA256: artifact.SHA256, BinarySHA256: artifact.BinarySHA256, Executable: name, ManifestSHA256: l.ManifestSHA256}
	root, err := m.root()
	if err != nil {
		return nil, err
	}
	dest := filepath.Join(root, l.Release, platform, artifact.BinarySHA256)
	receipt.Path = filepath.Join(dest, name)
	if _, err = os.Stat(dest); err == nil {
		return receipt, verifyInstallation(dest, *receipt)
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if !m.downloads() && m.Options.ArchivePath == "" {
		return nil, fmt.Errorf("runtime %s %s is not cached; downloads disabled", l.Release, platform)
	}
	unlock, err := m.acquire(ctx, root, l.Release+"-"+platform)
	if err != nil {
		return nil, err
	}
	defer unlock()
	if _, err = os.Stat(dest); err == nil {
		return receipt, verifyInstallation(dest, *receipt)
	}
	staging := filepath.Join(root, ".staging", token())
	if err = os.MkdirAll(staging, 0700); err != nil {
		return nil, err
	}
	defer os.RemoveAll(staging)
	var archive []byte
	if m.Options.ArchivePath != "" {
		archive, err = os.ReadFile(m.Options.ArchivePath)
	} else {
		archive, err = m.get(ctx, l.BaseURL+"/"+artifact.Archive, artifact.Size)
	}
	if err != nil {
		return nil, err
	}
	if int64(len(archive)) != artifact.Size || checksum(archive) != artifact.SHA256 {
		return nil, errors.New("runtime archive integrity mismatch")
	}
	tree := filepath.Join(staging, "tree")
	if err = extractArchive(archive, artifact.Archive, tree, "mimic-"+l.Release+"-"+platform); err != nil {
		return nil, err
	}
	bin := filepath.Join(tree, name)
	digest, err := fileDigest(bin)
	if err != nil || digest != artifact.BinarySHA256 {
		return nil, errors.New("runtime executable integrity mismatch")
	}
	if err = os.Chmod(bin, 0700); err != nil {
		return nil, err
	}
	raw, _ := json.Marshal(receipt)
	if err = os.WriteFile(filepath.Join(tree, "installation.json"), raw, 0600); err != nil {
		return nil, err
	}
	if err = os.MkdirAll(filepath.Dir(dest), 0700); err != nil {
		return nil, err
	}
	if err = os.Rename(tree, dest); err != nil {
		return nil, err
	}
	manifestDir := filepath.Join(root, ".manifests")
	if err = os.MkdirAll(manifestDir, 0700); err == nil {
		raw, _ = json.Marshal(l)
		err = publishManifest(filepath.Join(manifestDir, l.Release+".json"), raw)
	}
	if err != nil {
		return nil, err
	}
	return receipt, nil
}
func verifyInstallation(dest string, want Installation) error {
	raw, err := os.ReadFile(filepath.Join(dest, "installation.json"))
	if err != nil {
		return fmt.Errorf("incomplete cached installation: %w", err)
	}
	var got Installation
	if err = json.Unmarshal(raw, &got); err != nil {
		return err
	}
	want.Path = ""
	if got != want {
		return errors.New("cached installation provenance mismatch")
	}
	info, err := os.Lstat(filepath.Join(dest, want.Executable))
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("cached executable is not a regular file")
	}
	digest, err := fileDigest(filepath.Join(dest, want.Executable))
	if err != nil {
		return err
	}
	if digest != want.BinarySHA256 {
		return errors.New("cached executable hash mismatch")
	}
	return nil
}
func (m *RuntimeManager) acquire(ctx context.Context, root, key string) (func(), error) {
	locks := filepath.Join(root, ".locks")
	if err := os.MkdirAll(locks, 0700); err != nil {
		return nil, err
	}
	dir := filepath.Join(locks, key+".lock")
	timeout := m.Options.LockTimeout
	if timeout == 0 {
		timeout = 2 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	for {
		err := os.Mkdir(dir, 0700)
		if err == nil {
			hostname, _ := os.Hostname()
			ownerToken := token()
			raw, _ := json.Marshal(map[string]any{"pid": os.Getpid(), "hostname": hostname, "token": ownerToken, "createdAt": time.Now().UTC().Format(time.RFC3339Nano)})
			if err = os.WriteFile(filepath.Join(dir, "owner.json"), raw, 0600); err != nil {
				_ = os.Remove(dir)
				return nil, err
			}
			return func() {
				current, e := os.ReadFile(filepath.Join(dir, "owner.json"))
				var owner struct {
					Token string `json:"token"`
				}
				if e == nil && json.Unmarshal(current, &owner) == nil && owner.Token == ownerToken {
					_ = os.Remove(filepath.Join(dir, "owner.json"))
					_ = os.Remove(dir)
				}
			}, nil
		}
		if !os.IsExist(err) {
			return nil, err
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("installation lock %s: %w; inspect its owner before repairing a stale lock", dir, ctx.Err())
		case <-time.After(50 * time.Millisecond):
		}
	}
}
func extractArchive(data []byte, filename, dest, prefix string) error {
	if err := os.MkdirAll(dest, 0700); err != nil {
		return err
	}
	seen := map[string]bool{}
	var total int64
	write := func(name string, mode os.FileMode, size int64, r io.Reader) error {
		if strings.Contains(name, "\\") || strings.Contains(name, ":") || strings.HasPrefix(name, "/") || path.Clean(name) != strings.TrimSuffix(name, "/") {
			return errors.New("unsafe archive path")
		}
		if name == prefix+"/" {
			return nil
		}
		if !strings.HasPrefix(name, prefix+"/") {
			return errors.New("unexpected archive root")
		}
		rel := strings.TrimPrefix(name, prefix+"/")
		if rel == "" || strings.HasPrefix(rel, "../") || seen[rel] {
			return errors.New("duplicate or unsafe archive entry")
		}
		seen[rel] = true
		p := filepath.Join(dest, filepath.FromSlash(rel))
		if mode.IsDir() {
			return os.MkdirAll(p, 0700)
		}
		if !mode.IsRegular() {
			return errors.New("archive links and special entries are unsupported")
		}
		total += size
		if size < 0 || total > 2<<30 {
			return errors.New("extracted runtime exceeds size limit")
		}
		if err := os.MkdirAll(filepath.Dir(p), 0700); err != nil {
			return err
		}
		f, err := os.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		n, err := io.Copy(f, io.LimitReader(r, size+1))
		closeErr := f.Close()
		if err != nil {
			return err
		}
		if n != size {
			return errors.New("archive entry length mismatch")
		}
		return closeErr
	}
	if strings.HasSuffix(filename, ".zip") {
		z, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if err != nil {
			return err
		}
		for _, f := range z.File {
			r, err := f.Open()
			if err != nil {
				return err
			}
			err = write(f.Name, f.Mode(), int64(f.UncompressedSize64), r)
			_ = r.Close()
			if err != nil {
				return err
			}
		}
	} else {
		gz, err := gzip.NewReader(bytes.NewReader(data))
		if err != nil {
			return err
		}
		defer gz.Close()
		tr := tar.NewReader(gz)
		for {
			h, err := tr.Next()
			if err == io.EOF {
				break
			}
			if err != nil {
				return err
			}
			if h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeDir {
				return errors.New("archive links and special entries are unsupported")
			}
			if err = write(h.Name, h.FileInfo().Mode(), h.Size, tr); err != nil {
				return err
			}
		}
	}
	return nil
}

type RuntimeProcess struct {
	Endpoint   string
	Executable string
	Transport  *Transport
	Mimic      *Client
	cmd        *exec.Cmd
	done       chan struct{}
	exitErr    error
	lease      string
	closeOnce  sync.Once
	logMu      sync.Mutex
	log        string
}

func (p *RuntimeProcess) appendLog(s string) {
	p.logMu.Lock()
	defer p.logMu.Unlock()
	p.log += s + "\n"
	if len(p.log) > 16384 {
		p.log = p.log[len(p.log)-16384:]
	}
}
func (p *RuntimeProcess) Diagnostics() string { p.logMu.Lock(); defer p.logMu.Unlock(); return p.log }
func (p *RuntimeProcess) Close() error {
	p.closeOnce.Do(func() {
		if p.Transport != nil {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_, _ = p.Transport.CallRaw(ctx, "Browser.close", map[string]any{}, "")
			cancel()
			_ = p.Transport.Close()
		}
		select {
		case <-p.done:
		case <-time.After(2 * time.Second):
			_ = p.cmd.Process.Kill()
			<-p.done
		}
		if p.lease != "" {
			_ = os.Remove(p.lease)
		}
	})
	return nil
}
func (m *RuntimeManager) Launch(ctx context.Context) (*RuntimeProcess, error) {
	platform, err := hostPlatform()
	if err != nil {
		return nil, err
	}
	bin := m.Options.ExecutablePath
	if bin == "" {
		bin = os.Getenv("MIMIC_EXECUTABLE_PATH")
	}
	var installation *Installation
	wanted := m.Options.Version
	if wanted == "" && m.Options.LockFile == "" {
		wanted = os.Getenv("MIMIC_RUNTIME_VERSION")
	}
	if wanted != "" {
		wanted, err = normalizeVersion(wanted)
		if err != nil {
			return nil, err
		}
	}
	if bin == "" {
		installation, err = m.Install(ctx)
		if err != nil {
			return nil, err
		}
		bin = installation.Path
		root, e := m.root()
		if e != nil {
			return nil, e
		}
		unlock, e := m.acquire(ctx, root, installation.Release+"-"+installation.Platform)
		if e != nil {
			return nil, e
		}
		defer unlock()
		// Pruning takes this same lock. Recheck after Install's lock was
		// released, and retain ownership until the child lease is durable.
		if e = verifyInstallation(filepath.Dir(bin), *installation); e != nil {
			return nil, e
		}
		wanted = installation.Release
	} else {
		bin, err = filepath.Abs(bin)
		if err != nil {
			return nil, err
		}
		info, e := os.Stat(bin)
		if e != nil || !info.Mode().IsRegular() {
			return nil, fmt.Errorf("invalid explicit runtime executable %s", bin)
		}
		if m.Options.LockFile != "" {
			lock, e := m.ResolveLock(ctx)
			if e != nil {
				return nil, e
			}
			manifest, _ := lock.Validate()
			digest, e := fileDigest(bin)
			if e != nil {
				return nil, e
			}
			matched := false
			for _, artifact := range manifest.Artifacts {
				if artifact.Platform == platform && artifact.BinarySHA256 == digest {
					matched = true
				}
			}
			if !matched {
				return nil, errors.New("explicit runtime executable does not match the pinned lock hash")
			}
			wanted = lock.Release
		}
	}
	timeout := m.Options.StartupTimeout
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	startup, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.Command(bin, "--browser-mode", "headless", "--listen", "127.0.0.1:0")
	configureProcess(cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	p := &RuntimeProcess{cmd: cmd, Executable: bin, done: make(chan struct{})}
	if err = cmd.Start(); err != nil {
		return nil, err
	}
	ready := make(chan string, 1)
	var readers sync.WaitGroup
	readers.Add(2)
	scan := func(r io.Reader, startupLine bool) {
		defer readers.Done()
		s := bufio.NewScanner(r)
		s.Buffer(make([]byte, 4096), 1<<20)
		for s.Scan() {
			line := s.Text()
			p.appendLog(line)
			if startupLine && strings.HasPrefix(line, "Mimic listening on http://127.0.0.1:") {
				endpoint := strings.TrimPrefix(line, "Mimic listening on ")
				port := strings.TrimPrefix(endpoint, "http://127.0.0.1:")
				if n, e := strconv.Atoi(port); e == nil && n > 0 && n <= 65535 {
					select {
					case ready <- endpoint:
					default:
					}
				}
			}
		}
	}
	go scan(stdout, true)
	go scan(stderr, false)
	go func() { readers.Wait(); p.exitErr = cmd.Wait(); close(p.done) }()
	success := false
	defer func() {
		if !success {
			_ = p.Close()
		}
	}()
	select {
	case p.Endpoint = <-ready:
	case <-p.done:
		return nil, fmt.Errorf("runtime exited: %v\n%s", p.exitErr, p.Diagnostics())
	case <-startup.Done():
		return nil, fmt.Errorf("runtime startup: %w\n%s", startup.Err(), p.Diagnostics())
	}
	p.Transport, err = Dial(startup, p.Endpoint)
	if err != nil {
		return nil, err
	}
	p.Mimic = NewClient(p.Transport, "")
	var identity struct {
		Version       string `json:"version"`
		ChromeVersion string `json:"chromeVersion"`
	}
	if err = p.Mimic.Call(startup, "Mimic.getVersion", map[string]any{}, &identity); err != nil {
		return nil, fmt.Errorf("not a compatible Mimic runtime: %w", err)
	}
	if wanted != "" {
		wanted, err = normalizeVersion(wanted)
		if err != nil {
			return nil, err
		}
		if identity.Version != wanted && identity.Version != strings.TrimPrefix(wanted, "v") {
			return nil, fmt.Errorf("requested %s, runtime reports %s", wanted, identity.Version)
		}
	}
	if identity.Version == "" || identity.ChromeVersion == "" {
		return nil, errors.New("invalid Mimic identity")
	}
	if installation != nil {
		dir := filepath.Join(filepath.Dir(bin), ".leases")
		if err = os.MkdirAll(dir, 0700); err != nil {
			return nil, err
		}
		hostname, _ := os.Hostname()
		p.lease = filepath.Join(dir, token()+".json")
		raw, _ := json.Marshal(map[string]any{"launcherPid": os.Getpid(), "runtimePid": cmd.Process.Pid, "hostname": hostname, "createdAt": time.Now().UTC().Format(time.RFC3339Nano)})
		if err = os.WriteFile(p.lease, raw, 0600); err != nil {
			return nil, err
		}
	}
	success = true
	go func() {
		select {
		case <-ctx.Done():
			_ = p.Close()
		case <-p.done:
		}
	}()
	return p, nil
}
