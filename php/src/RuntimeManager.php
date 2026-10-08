<?php
declare(strict_types=1);
namespace Mimic\Sdk;

/** Native installer implementing the shared cross-language cache contract. */
final class RuntimeManager
{
    private const RELEASE_BASE = 'https://github.com/mimic-browser/runtime/releases/download/';

    public static function normalizeVersion(string $version): string
    {
        if (!preg_match('/^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?$/D', $version)) {
            throw new SdkException('configuration', 'Runtime version must be exact, never latest or a range');
        }
        return str_starts_with($version, 'v') ? $version : 'v' . $version;
    }
    public static function cacheRoot(?RuntimeOptions $options = null): string
    {
        $custom = $options?->runtimeDirectory ?? (getenv('MIMIC_RUNTIME_DIR') ?: null);
        if ($custom !== null) { return self::absolute($custom); }
        if (PHP_OS_FAMILY === 'Windows') {
            $local = getenv('LOCALAPPDATA') ?: (getenv('USERPROFILE') . '/AppData/Local');
            return self::absolute($local . '/Mimic/runtimes');
        }
        $cache = getenv('XDG_CACHE_HOME') ?: (getenv('HOME') . '/.cache');
        return self::absolute($cache . '/Mimic/runtimes');
    }
    public static function platform(): string
    {
        if (PHP_INT_SIZE !== 8 || !in_array(strtolower(php_uname('m')), ['x86_64', 'amd64'], true)) { throw new SdkException('platform', 'Mimic requires amd64'); }
        if (PHP_OS_FAMILY === 'Windows') { return 'windows-amd64'; }
        if (PHP_OS_FAMILY !== 'Linux') { throw new SdkException('platform', 'Mimic currently packages Windows and Linux amd64 only'); }
        $process = proc_open(['getconf', 'GNU_LIBC_VERSION'], [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, null, null, ['bypass_shell' => true]);
        if (!is_resource($process)) { throw new SdkException('platform', 'Cannot inspect glibc'); }
        fclose($pipes[0]); $version = trim(stream_get_contents($pipes[1])); fclose($pipes[1]); fclose($pipes[2]); $status = proc_close($process);
        if ($status !== 0 || !preg_match('/^glibc (\d+\.\d+)/', $version, $match) || version_compare($match[1], '2.39', '<')) { throw new SdkException('platform', 'Mimic requires glibc 2.39 or newer'); }
        return 'linux-amd64';
    }
    public static function defaultLock(): object { return self::readJson(dirname(__DIR__) . '/resources/runtime-lock.json'); }
    public function resolveLock(?RuntimeOptions $options = null): object
    {
        $options ??= new RuntimeOptions(); $default = self::defaultLock();
        $lock = $options->lockFile === null ? null : self::readJson($options->lockFile);
        $version = self::normalizeVersion($options->version ?? $lock?->release ?? (getenv('MIMIC_RUNTIME_VERSION') ?: $default->release));
        if ($lock !== null) { self::validateLock($lock, $version); return $lock; }
        if ($default->release === $version) { self::validateLock($default, $version); return $default; }
        $metadata = self::cacheRoot($options) . '/.manifests/' . $version . '.json';
        if (is_file($metadata)) { $lock = self::readJson($metadata); self::validateLock($lock, $version); return $lock; }
        self::ensureDownload($options, $version);
        $base = self::RELEASE_BASE . $version;
        $bytes = self::download($base . '/release-manifest.json', 4 * 1024 * 1024);
        $digest = hash('sha256', $bytes);
        $sums = self::download($base . '/SHA256SUMS', 4 * 1024 * 1024);
        if (!preg_match('/^' . preg_quote($digest, '/') . '\s+\*?release-manifest\.json\s*$/mi', $sums)) { throw new SdkException('integrity', 'Manifest checksum missing or incorrect'); }
        $lock = (object) ['release' => $version, 'manifestSha256' => $digest, 'manifestJson' => $bytes, 'manifest' => json_decode($bytes, false, 512, JSON_THROW_ON_ERROR), 'baseUrl' => $base];
        self::validateLock($lock, $version); self::mkdir(dirname($metadata));
        $temporary = $metadata . '.' . self::uuid() . '.tmp';
        try {
            self::writeJson($temporary, $lock);
            if (!@link($temporary, $metadata)) {
                if (!is_file($metadata) || self::readJson($metadata) != $lock) { throw new SdkException('integrity', 'Conflicting cached release provenance'); }
            }
        } finally { if (is_file($temporary)) { unlink($temporary); } }
        return $lock;
    }
    public static function validateLock(object $lock, ?string $selected = null): void
    {
        $release = self::normalizeVersion($lock->release ?? '');
        if ($selected !== null && $selected !== $release) { throw new SdkException('configuration', 'Explicit version conflicts with lock file'); }
        $manifest = $lock->manifest ?? null;
        if (!is_object($manifest) || ($manifest->version ?? '') !== $release || !preg_match('/^[0-9a-f]{40}$/D', $manifest->sourceRevision ?? '')) { throw new SdkException('integrity', 'Invalid manifest release/source identity'); }
        self::validateHash($lock->manifestSha256 ?? '');
        if (($lock->baseUrl ?? '') !== self::RELEASE_BASE . $release) { throw new SdkException('integrity', 'Manifest URL must be the selected official release'); }
        if (!is_string($lock->manifestJson ?? null) || hash('sha256', $lock->manifestJson) !== $lock->manifestSha256 || json_decode($lock->manifestJson, false, 512, JSON_THROW_ON_ERROR) != $manifest) {
            throw new SdkException('integrity', 'Manifest bytes do not match digest and document');
        }
        if (!is_array($manifest->artifacts ?? null)) { throw new SdkException('integrity', 'Manifest artifacts missing'); }
        $seen = [];
        foreach ($manifest->artifacts as $artifact) {
            $platform = $artifact->platform ?? '';
            if (!in_array($platform, ['windows-amd64', 'linux-amd64'], true) || isset($seen[$platform])) { throw new SdkException('integrity', 'Invalid or duplicate artifact platform'); }
            $seen[$platform] = true;
            $name = "mimic-$release-$platform" . (str_starts_with($platform, 'windows') ? '.zip' : '.tar.gz');
            if (($artifact->archive ?? '') !== $name || !is_int($artifact->size ?? null) || $artifact->size <= 0) { throw new SdkException('integrity', 'Invalid artifact identity'); }
            self::validateHash($artifact->sha256 ?? ''); self::validateHash($artifact->binarySha256 ?? '');
        }
    }
    public function install(?RuntimeOptions $options = null): RuntimeInstallation
    {
        $options ??= new RuntimeOptions(); $platform = self::platform();
        $explicit = $options->executablePath ?? (getenv('MIMIC_EXECUTABLE_PATH') ?: null);
        $lock = $explicit !== null && $options->lockFile === null ? self::defaultLock() : $this->resolveLock($options);
        $release = $explicit !== null && $options->lockFile === null ? self::normalizeVersion($options->version ?? (getenv('MIMIC_RUNTIME_VERSION') ?: $lock->release)) : $lock->release;
        $artifact = self::artifact($lock, $platform);
        if ($explicit !== null) {
            $explicit = self::absolute($explicit);
            if (!is_file($explicit)) { throw new SdkException('executable', 'Explicit executable does not exist: ' . $explicit); }
            if ($options->lockFile !== null && hash_file('sha256', $explicit) !== $artifact->binarySha256) { throw new SdkException('integrity', 'Explicit executable does not match locked artifact'); }
            return new RuntimeInstallation($explicit, $release, null, $lock);
        }
        $root = self::cacheRoot($options); $destination = "$root/$release/$platform/$artifact->binarySha256";
        $basename = $platform === 'windows-amd64' ? 'mimic.exe' : 'mimic';
        if (is_dir($destination)) { self::verify($destination, $lock, $platform); return new RuntimeInstallation("$destination/$basename", $release, $destination, $lock); }
        if ($options->archivePath === null) { self::ensureDownload($options, $release); }
        $guard = self::acquireLock($root, "$release-$platform", $options->lockTimeout);
        $staging = "$root/.staging/" . self::uuid();
        try {
            if (is_dir($destination)) { self::verify($destination, $lock, $platform); return new RuntimeInstallation("$destination/$basename", $release, $destination, $lock); }
            self::mkdir($staging); $archive = "$staging/download";
            if ($options->archivePath === null) { self::downloadFile($lock->baseUrl . '/' . $artifact->archive, $archive, $artifact->size); }
            else {
                if (filesize($options->archivePath) !== $artifact->size) { throw new SdkException('integrity', 'Local archive size mismatch'); }
                if (!copy($options->archivePath, $archive)) { throw new SdkException('io', 'Cannot stage local archive'); }
            }
            if (hash_file('sha256', $archive) !== $artifact->sha256) { throw new SdkException('integrity', 'Archive checksum mismatch'); }
            $tree = "$staging/tree"; self::mkdir($tree);
            self::extractArchive($archive, $tree, "mimic-$release-$platform", $platform === 'windows-amd64');
            $binary = "$tree/$basename";
            if (!is_file($binary) || hash_file('sha256', $binary) !== $artifact->binarySha256) { throw new SdkException('integrity', 'Executable checksum mismatch'); }
            if (PHP_OS_FAMILY === 'Linux' && !chmod($binary, 0755)) { throw new SdkException('permission', 'Cannot make runtime executable'); }
            self::writeJson("$tree/installation.json", self::receipt($lock, $platform));
            self::mkdir(dirname($destination));
            if (!rename($tree, $destination)) { throw new SdkException('io', 'Cannot atomically publish installation'); }
            return new RuntimeInstallation("$destination/$basename", $release, $destination, $lock);
        } finally { if (is_dir($staging)) { self::removeTree($staging); } self::releaseLock($guard); }
    }
    public function launch(?RuntimeOptions $options = null): RuntimeProcess
    {
        $options ??= new RuntimeOptions(); $installation = $this->install($options);
        if ($installation->directory === null) { return RuntimeProcess::start($installation, $options); }
        $platform = self::platform();
        $guard = self::acquireLock(self::cacheRoot($options), $installation->release . '-' . $platform, $options->lockTimeout);
        try { self::verify($installation->directory, $installation->runtimeLock, $platform); return RuntimeProcess::start($installation, $options); }
        finally { self::releaseLock($guard); }
    }
    public static function verify(string $directory, object $lock, string $platform): void
    {
        if (!is_file("$directory/installation.json")) { throw new SdkException('integrity', 'Incomplete installation: receipt missing'); }
        $expected = self::receipt($lock, $platform); $actual = self::readJson("$directory/installation.json");
        foreach (get_object_vars($expected) as $key => $value) { if (!property_exists($actual, $key) || $actual->$key !== $value) { throw new SdkException('integrity', 'Installation provenance mismatch: ' . $key); } }
        if (!is_file("$directory/$expected->executable") || hash_file('sha256', "$directory/$expected->executable") !== $expected->binarySha256) { throw new SdkException('integrity', 'Installed executable checksum mismatch'); }
    }
    public static function list(?RuntimeOptions $options = null): array
    {
        $root = self::cacheRoot($options); if (!is_dir($root)) { return []; }
        $result = [];
        foreach (new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root, \FilesystemIterator::SKIP_DOTS)) as $file) {
            if ($file->getFilename() === 'installation.json') { $result[] = $file->getPath(); }
        }
        return $result;
    }
    public static function prune(string $directory, ?RuntimeOptions $options = null): void
    {
        $root = realpath(self::cacheRoot($options)); $target = realpath($directory);
        if ($root === false || $target === false || !str_starts_with($target, $root . DIRECTORY_SEPARATOR) || !is_file("$target/installation.json")) { throw new SdkException('configuration', 'Prune only accepts complete installations inside this cache'); }
        $receipt = self::readJson("$target/installation.json"); $release = self::normalizeVersion($receipt->release ?? ''); $platform = $receipt->platform ?? '';
        if (!in_array($platform, ['windows-amd64', 'linux-amd64'], true)) { throw new SdkException('integrity', 'Invalid receipt platform'); }
        $guard = self::acquireLock($root, "$release-$platform", $options?->lockTimeout ?? 120.0);
        try {
            if (is_dir("$target/.leases") && count(scandir("$target/.leases")) > 2) { throw new SdkException('lease', 'Installation has leases; repair stale leases explicitly before pruning'); }
            self::removeTree($target);
        } finally { self::releaseLock($guard); }
    }
    public static function extractArchive(string $archive, string $destination, string $topLevel, bool $zip): void
    {
        $seen = [];
        $safe = static function (string $name, bool $directory) use ($destination, $topLevel, &$seen): ?string {
            $name = rtrim(str_replace('\\', '/', $name), '/');
            if (str_starts_with($name, '/') || str_contains($name, ':') || in_array('..', explode('/', $name), true) || str_contains($name, "\0")) { throw new SdkException('integrity', 'Unsafe archive path'); }
            if ($name === $topLevel && $directory) { return null; }
            if (str_starts_with($name, $topLevel . '/')) { $name = substr($name, strlen($topLevel) + 1); }
            $key = PHP_OS_FAMILY === 'Windows' ? strtolower($name) : $name;
            if ($name === '' || isset($seen[$key]) || in_array('.', explode('/', $name), true) || in_array('', explode('/', $name), true)) { throw new SdkException('integrity', 'Duplicate or invalid archive path'); }
            $seen[$key] = true; return $destination . '/' . $name;
        };
        if ($zip) {
            $input = new \ZipArchive(); if ($input->open($archive) !== true) { throw new SdkException('integrity', 'Invalid zip archive'); }
            try {
                for ($i = 0; $i < $input->numFiles; $i++) {
                    $name = $input->getNameIndex($i); $input->getExternalAttributesIndex($i, $system, $attributes);
                    if ((($attributes >> 16) & 0170000) === 0120000) { throw new SdkException('integrity', 'Archive symlinks are forbidden'); }
                    $directory = str_ends_with($name, '/'); $path = $safe($name, $directory); if ($path === null) { continue; }
                    if ($directory) { self::mkdir($path); continue; }
                    self::mkdir(dirname($path)); $stream = $input->getStream($name); $output = fopen($path, 'xb');
                    if ($stream === false || $output === false) { throw new SdkException('integrity', 'Cannot extract zip entry'); }
                    try { stream_copy_to_stream($stream, $output); } finally { fclose($stream); fclose($output); }
                }
            } finally { $input->close(); }
            return;
        }
        $input = gzopen($archive, 'rb'); if ($input === false) { throw new SdkException('integrity', 'Invalid gzip archive'); }
        $read = static function (int $size) use ($input): string {
            $result = ''; while (strlen($result) < $size) { $part = gzread($input, $size - strlen($result)); if ($part === false || $part === '') { throw new SdkException('integrity', 'Truncated tar archive'); } $result .= $part; } return $result;
        };
        $pax = []; $longName = null;
        try {
            while (true) {
                $header = $read(512); if ($header === str_repeat("\0", 512)) { break; }
                $checksum = octdec(trim(substr($header, 148, 8), "\0 "));
                $computed = array_sum(unpack('C*', substr_replace($header, str_repeat(' ', 8), 148, 8)));
                if ($checksum !== $computed) { throw new SdkException('integrity', 'Invalid tar header checksum'); }
                $sizeText = trim(substr($header, 124, 12), "\0 ");
                if (!preg_match('/^[0-7]*$/D', $sizeText)) { throw new SdkException('integrity', 'Unsupported tar entry size'); }
                $size = (int) octdec($sizeText); if ($size > 1024 * 1024 * 1024) { throw new SdkException('integrity', 'Tar entry exceeds 1 GiB'); }
                $type = $header[156]; $name = rtrim(substr($header, 0, 100), "\0"); $prefix = rtrim(substr($header, 345, 155), "\0");
                if ($prefix !== '') { $name = $prefix . '/' . $name; }
                if ($type === 'x' || $type === 'g' || $type === 'L') {
                    if ($size > 1024 * 1024) { throw new SdkException('integrity', 'Tar metadata too large'); }
                    $data = $read($size); if ($size % 512 !== 0) { $read(512 - ($size % 512)); }
                    if ($type === 'L') { $longName = rtrim($data, "\0\n"); continue; }
                    while ($data !== '') {
                        $space = strpos($data, ' '); if ($space === false) { throw new SdkException('integrity', 'Invalid PAX metadata'); }
                        $length = (int) substr($data, 0, $space); if ($length <= $space + 1 || $length > strlen($data)) { throw new SdkException('integrity', 'Invalid PAX length'); }
                        $pair = explode('=', rtrim(substr($data, $space + 1, $length - $space - 1), "\n"), 2);
                        if (count($pair) === 2 && $type === 'x') { $pax[$pair[0]] = $pair[1]; }
                        if (isset($pair[0]) && $pair[0] === 'linkpath') { throw new SdkException('integrity', 'Archive links are forbidden'); }
                        $data = substr($data, $length);
                    }
                    continue;
                }
                if (!in_array($type, ["\0", '0', '5'], true)) { throw new SdkException('integrity', 'Archive links and special entries are forbidden'); }
                $name = $pax['path'] ?? $longName ?? $name; $pax = []; $longName = null;
                $directory = $type === '5'; $path = $safe($name, $directory);
                if ($path !== null && $directory) { self::mkdir($path); }
                $output = null;
                if ($path !== null && !$directory) { self::mkdir(dirname($path)); $output = fopen($path, 'xb'); if ($output === false) { throw new SdkException('io', 'Cannot extract tar entry'); } }
                try { for ($remaining = $size; $remaining > 0; $remaining -= $length) { $length = min(65536, $remaining); $data = $read($length); if (is_resource($output) && fwrite($output, $data) !== strlen($data)) { throw new SdkException('io', 'Incomplete extracted entry'); } } }
                finally { if (is_resource($output)) { fclose($output); } }
                if ($size % 512 !== 0) { $read(512 - ($size % 512)); }
            }
        } finally { gzclose($input); }
    }
    private static function artifact(object $lock, string $platform): object
    {
        foreach ($lock->manifest->artifacts as $artifact) { if ($artifact->platform === $platform) { return $artifact; } }
        throw new SdkException('platform', 'Release has no artifact for ' . $platform);
    }
    private static function receipt(object $lock, string $platform): object
    {
        $artifact = self::artifact($lock, $platform);
        return (object) ['release' => $lock->release, 'platform' => $platform, 'sourceRevision' => $lock->manifest->sourceRevision, 'archiveSha256' => $artifact->sha256, 'binarySha256' => $artifact->binarySha256, 'executable' => $platform === 'windows-amd64' ? 'mimic.exe' : 'mimic', 'manifestSha256' => $lock->manifestSha256];
    }
    private static function validateHash(string $value): void { if (!preg_match('/^[0-9a-f]{64}$/D', $value)) { throw new SdkException('integrity', 'Invalid SHA256 digest'); } }
    private static function ensureDownload(RuntimeOptions $options, string $release): void { if (!$options->allowDownload || getenv('MIMIC_DOWNLOAD') === '0') { throw new SdkException('offline', 'Runtime ' . $release . ' is missing; enable downloads and call install first'); } }
    private static function download(string $url, int $limit): string
    {
        $result = '';
        self::transfer($url, $limit, static function (string $part) use (&$result): void { $result .= $part; });
        return $result;
    }
    private static function downloadFile(string $url, string $target, int $size): void
    {
        $output = fopen($target, 'xb'); if ($output === false) { throw new SdkException('permission', 'Cannot write archive'); }
        try { $total = self::transfer($url, $size, static function (string $part) use ($output): void { if (fwrite($output, $part) !== strlen($part)) { throw new SdkException('io', 'Archive write failed'); } }); }
        finally { fclose($output); }
        if ($total !== $size) { throw new SdkException('integrity', 'Archive size mismatch'); }
    }
    private static function transfer(string $url, int $limit, callable $write): int
    {
        // libcurl honors standard proxy/CA configuration; the timeout bounds the whole transfer.
        $handle = curl_init($url); $total = 0; $failure = null;
        curl_setopt_array($handle, [CURLOPT_FOLLOWLOCATION => true, CURLOPT_MAXREDIRS => 8, CURLOPT_PROTOCOLS => CURLPROTO_HTTPS, CURLOPT_REDIR_PROTOCOLS => CURLPROTO_HTTPS, CURLOPT_CONNECTTIMEOUT => 20, CURLOPT_TIMEOUT => 120, CURLOPT_FAILONERROR => true, CURLOPT_USERAGENT => 'Mimic-SDK-PHP/0.1.1', CURLOPT_WRITEFUNCTION => static function ($unused, string $part) use (&$total, &$failure, $limit, $write): int {
            try { $total += strlen($part); if ($total > $limit) { throw new SdkException('integrity', 'Download exceeds declared size'); } $write($part); return strlen($part); }
            catch (\Throwable $error) { $failure = $error; return 0; }
        }]);
        try {
            if (curl_exec($handle) === false) { throw $failure ?? new SdkException('download', 'Official artifact download failed: ' . curl_error($handle)); }
        } finally { curl_close($handle); }
        return $total;
    }
    public static function acquireLock(string $root, string $key, float $timeout): array
    {
        $path = "$root/.locks/$key.lock"; self::mkdir(dirname($path)); $token = self::uuid(); $deadline = microtime(true) + $timeout;
        while (!@mkdir($path, 0700)) {
            if (!is_dir($path)) { throw new SdkException('permission', 'Cannot create installation lock: ' . $path); }
            if (microtime(true) >= $deadline) { throw new SdkException('lock', 'Installation lock timed out: ' . $path); }
            usleep(100000); clearstatcache(true, $path);
        }
        try { self::writeJson("$path/owner.json", (object) ['pid' => getmypid(), 'hostname' => gethostname(), 'token' => $token, 'createdAt' => gmdate('c')]); }
        catch (\Throwable $error) { self::removeTree($path); throw $error; }
        return [$path, $token];
    }
    public static function releaseLock(array $guard): void
    {
        [$path, $token] = $guard;
        if (is_file("$path/owner.json") && (self::readJson("$path/owner.json")->token ?? null) === $token) { self::removeTree($path); }
    }
    public static function readJson(string $path): object
    {
        $bytes = @file_get_contents($path); if ($bytes === false) { throw new SdkException('io', 'Cannot read JSON: ' . $path); }
        $value = json_decode($bytes, false, 512, JSON_THROW_ON_ERROR); if (!is_object($value)) { throw new SdkException('integrity', 'Expected JSON object: ' . $path); } return $value;
    }
    public static function writeJson(string $path, object $value): void
    {
        $bytes = json_encode($value, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
        if (file_put_contents($path, $bytes) !== strlen($bytes)) { throw new SdkException('io', 'Cannot write JSON: ' . $path); }
    }
    public static function mkdir(string $path): void { if (!is_dir($path) && !@mkdir($path, 0700, true) && !is_dir($path)) { throw new SdkException('permission', 'Cannot create directory: ' . $path); } }
    public static function removeTree(string $path): void
    {
        if (is_link($path) || is_file($path)) { if (!unlink($path)) { throw new SdkException('io', 'Cannot remove owned path'); } return; }
        if (!is_dir($path)) { return; }
        foreach (scandir($path) as $entry) { if ($entry !== '.' && $entry !== '..') { self::removeTree($path . '/' . $entry); } }
        if (!rmdir($path)) { throw new SdkException('io', 'Cannot remove owned directory'); }
    }
    public static function absolute(string $path): string
    {
        if ($path === '') { throw new SdkException('configuration', 'Path must not be empty'); }
        if (!str_starts_with($path, '/') && !preg_match('/^[A-Za-z]:[\\\\\/]/', $path) && !str_starts_with($path, '\\\\')) { $path = getcwd() . DIRECTORY_SEPARATOR . $path; }
        $trimmed = rtrim($path, '/\\');
        return $trimmed === '' || preg_match('/^[A-Za-z]:$/D', $trimmed) ? $trimmed . DIRECTORY_SEPARATOR : $trimmed;
    }
    public static function uuid(): string
    {
        $bytes = random_bytes(16); $bytes[6] = chr((ord($bytes[6]) & 15) | 64); $bytes[8] = chr((ord($bytes[8]) & 63) | 128); $hex = bin2hex($bytes);
        return substr($hex, 0, 8) . '-' . substr($hex, 8, 4) . '-' . substr($hex, 12, 4) . '-' . substr($hex, 16, 4) . '-' . substr($hex, 20);
    }
}
