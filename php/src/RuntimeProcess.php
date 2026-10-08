<?php
declare(strict_types=1);
namespace Mimic\Sdk;

/** Owns only a launched process; a connected instance owns its socket alone. */
final class RuntimeProcess
{
    private bool $closed = false;
    private function __construct(
        public readonly string $endpoint,
        public readonly string $webSocketEndpoint,
        public readonly object $identity,
        public readonly ProtocolConnection $transport,
        private mixed $process = null,
        public readonly ?int $processId = null,
        private readonly ?string $lease = null,
        private readonly ?string $logs = null,
    ) {
        $weak = \WeakReference::create($this);
        register_shutdown_function(static function () use ($weak): void { $weak->get()?->close(); });
    }
    public function isOwned(): bool { return is_resource($this->process); }
    public static function connect(string $endpoint): self
    {
        $websocket = self::discover($endpoint); $transport = ProtocolConnection::connect($websocket);
        try { $identity = $transport->send('Mimic.getVersion'); self::validateIdentity($identity, null); return new self($endpoint, $websocket, $identity, $transport); }
        catch (\Throwable $error) { $transport->close(); throw $error; }
    }
    public static function start(RuntimeInstallation $installation, RuntimeOptions $options): self
    {
        $arguments = [$installation->executablePath, '--browser-mode', 'headless', '--listen', '127.0.0.1:0'];
        foreach ($options->arguments as $argument) {
            if (!is_string($argument) || preg_match('/^--?(listen|browser-mode)(=.*)?$/D', $argument)) { throw new SdkException('configuration', 'Arguments cannot override loopback/headless mode'); }
            $arguments[] = $argument;
        }
        $logs = sys_get_temp_dir() . '/mimic-sdk-php-' . RuntimeManager::uuid(); RuntimeManager::mkdir($logs);
        $null = PHP_OS_FAMILY === 'Windows' ? 'NUL' : '/dev/null';
        $process = proc_open($arguments, [0 => ['file', $null, 'r'], 1 => ['file', "$logs/stdout", 'w'], 2 => ['file', "$logs/stderr", 'w']], $pipes, null, null, ['bypass_shell' => true, 'create_new_console' => false]);
        if (!is_resource($process)) { RuntimeManager::removeTree($logs); throw new SdkException('startup', 'Cannot start Mimic process'); }
        $pid = proc_get_status($process)['pid']; $deadline = microtime(true) + $options->timeout; $transport = null; $lease = null;
        try {
            $endpoint = null;
            while ($endpoint === null) {
                $output = @file_get_contents("$logs/stdout") ?: '';
                if (preg_match('/^Mimic listening on (http:\/\/127\.0\.0\.1:[1-9]\d*)\r?$/m', $output, $match)) { $endpoint = $match[1]; break; }
                if (!proc_get_status($process)['running']) { throw new SdkException('startup', 'Mimic exited before readiness'); }
                if (microtime(true) >= $deadline) { throw new SdkException('timeout', 'Mimic startup timed out'); }
                usleep(20000);
            }
            $websocket = self::discover($endpoint, self::remaining($deadline));
            $transport = ProtocolConnection::connect($websocket, self::remaining($deadline));
            $transport->timeout = self::remaining($deadline);
            $identity = $transport->send('Mimic.getVersion');
            $enforce = $installation->directory !== null || $options->version !== null || $options->lockFile !== null || getenv('MIMIC_RUNTIME_VERSION') !== false;
            self::validateIdentity($identity, $enforce ? $installation->release : null);
            if ($installation->directory !== null) {
                RuntimeManager::mkdir($installation->directory . '/.leases');
                $lease = $installation->directory . '/.leases/' . RuntimeManager::uuid() . '.json';
                RuntimeManager::writeJson($lease, (object) ['launcherPid' => getmypid(), 'runtimePid' => $pid, 'hostname' => gethostname(), 'createdAt' => gmdate('c')]);
            }
            $transport->timeout = 30;
            return new self($endpoint, $websocket, $identity, $transport, $process, $pid, $lease, $logs);
        } catch (\Throwable $error) {
            $transport?->close(); proc_terminate($process, 9); proc_close($process);
            $output = substr((@file_get_contents("$logs/stdout") ?: '') . "\n" . (@file_get_contents("$logs/stderr") ?: ''), -32768);
            if ($lease !== null && is_file($lease)) { unlink($lease); }
            RuntimeManager::removeTree($logs);
            throw new SdkException('startup', 'Mimic startup failed: ' . $error->getMessage() . "\n" . $output, $error);
        }
    }
    private static function remaining(float $deadline): float
    {
        $remaining = $deadline - microtime(true);
        if ($remaining <= 0) { throw new SdkException('timeout', 'Mimic startup timed out'); }
        return $remaining;
    }
    private static function discover(string $endpoint, float $timeout = 15): string
    {
        $url = parse_url($endpoint);
        if ($url === false || !isset($url['scheme'], $url['host'])) { throw new SdkException('configuration', 'Endpoint must be an absolute URL'); }
        if (in_array($url['scheme'], ['ws', 'wss'], true)) { return $endpoint; }
        if (!in_array($url['scheme'], ['http', 'https'], true)) { throw new SdkException('configuration', 'Endpoint must use HTTP(S) or WS(S)'); }
        $origin = $url['scheme'] . '://' . $url['host'] . (isset($url['port']) ? ':' . $url['port'] : '');
        $handle = curl_init($origin . '/json/version'); $bytes = '';
        curl_setopt_array($handle, [CURLOPT_TIMEOUT_MS => max(1, (int) ($timeout * 1000)), CURLOPT_FAILONERROR => true, CURLOPT_WRITEFUNCTION => static function ($unused, string $part) use (&$bytes): int {
            if (strlen($bytes) + strlen($part) > 1024 * 1024) { return 0; }
            $bytes .= $part; return strlen($part);
        }]);
        try { if (curl_exec($handle) === false) { throw new SdkException('connection', 'Cannot discover Mimic endpoint: ' . curl_error($handle)); } }
        finally { curl_close($handle); }
        $version = json_decode($bytes, false, 512, JSON_THROW_ON_ERROR); $websocket = $version->webSocketDebuggerUrl ?? ''; $target = parse_url($websocket);
        if ($target === false || !in_array($target['scheme'] ?? '', ['ws', 'wss'], true) || strcasecmp($target['host'] ?? '', $url['host']) !== 0) { throw new SdkException('identity', 'Discovery returned unrelated websocket endpoint'); }
        return $websocket;
    }
    private static function validateIdentity(object $identity, ?string $expected): void
    {
        if (!is_string($identity->version ?? null) || $identity->version === '' || !isset($identity->chromeVersion, $identity->baseProfile)) { throw new SdkException('identity', 'Endpoint is not a compatible Mimic runtime'); }
        if ($expected !== null && $identity->version !== $expected && 'v' . $identity->version !== $expected) { throw new SdkException('identity', "Expected Mimic $expected, received $identity->version"); }
    }
    public function close(): void
    {
        if ($this->closed) { return; } $this->closed = true;
        try {
            if (is_resource($this->process)) {
                if (proc_get_status($this->process)['running']) {
                    $this->transport->timeout = 3;
                    try { $this->transport->send('Browser.close'); } catch (\Throwable) { /* Shutdown can close its socket before the response. */ }
                    $deadline = microtime(true) + 3;
                    while (proc_get_status($this->process)['running'] && microtime(true) < $deadline) { usleep(20000); }
                    if (proc_get_status($this->process)['running']) { proc_terminate($this->process, 9); }
                }
                proc_close($this->process); $this->process = null;
                if ($this->lease !== null && is_file($this->lease)) { unlink($this->lease); }
            }
        } finally {
            $this->transport->close();
            if ($this->logs !== null && is_dir($this->logs)) { RuntimeManager::removeTree($this->logs); }
        }
    }
    public function __destruct() { $this->close(); }
}
