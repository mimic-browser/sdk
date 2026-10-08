<?php
declare(strict_types=1);
namespace Mimic\Sdk;

/** One explicit raw CDP websocket, shared by typed and experimental commands. */
final class ProtocolConnection implements ProtocolTransport
{
    private mixed $socket;
    private int $sequence = 0;
    private bool $closed = false;
    private array $responses = [];
    private array $events = [];

    private function __construct(mixed $socket, public float $timeout = 30.0) { $this->socket = $socket; }

    public static function connect(string $endpoint, float $timeout = 30.0): self
    {
        $url = parse_url($endpoint);
        if ($url === false || !isset($url['host'], $url['scheme']) || !in_array($url['scheme'], ['ws', 'wss'], true)) {
            throw new SdkException('configuration', 'CDP endpoint must be WS or WSS');
        }
        $secure = $url['scheme'] === 'wss';
        $port = $url['port'] ?? ($secure ? 443 : 80);
        $host = $url['host'];
        $context = stream_context_create(['ssl' => ['verify_peer' => true, 'verify_peer_name' => true, 'peer_name' => $host]]);
        $socket = @stream_socket_client(($secure ? 'tls' : 'tcp') . '://' . $host . ':' . $port, $errno, $error, $timeout, STREAM_CLIENT_CONNECT, $context);
        if ($socket === false) { throw new SdkException('connection', 'Cannot connect CDP socket: ' . $error); }
        stream_set_timeout($socket, (int) $timeout, (int) (($timeout - (int) $timeout) * 1_000_000));
        $connection = new self($socket, $timeout);
        try {
            $key = base64_encode(random_bytes(16));
            $path = ($url['path'] ?? '/') . (isset($url['query']) ? '?' . $url['query'] : '');
            if (strpbrk($path . $host, "\r\n") !== false) { throw new SdkException('configuration', 'Invalid endpoint characters'); }
            $connection->writeAll("GET $path HTTP/1.1\r\nHost: $host:$port\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n\r\n");
            $status = fgets($socket);
            if ($status === false || !preg_match('/^HTTP\/1\.[01] 101\b/', $status)) { throw new SdkException('connection', 'CDP websocket upgrade rejected'); }
            $headers = [];
            $length = 0;
            while (($line = fgets($socket)) !== false && $line !== "\r\n") {
                $length += strlen($line);
                if ($length > 65536) { throw new SdkException('protocol', 'Websocket headers too large'); }
                $pair = explode(':', trim($line), 2);
                if (count($pair) === 2) { $headers[strtolower($pair[0])] = trim($pair[1]); }
            }
            $expected = base64_encode(sha1($key . '258EAFA5-E914-47DA-95CA-C5AB0DC85B11', true));
            if (($headers['sec-websocket-accept'] ?? '') !== $expected) { throw new SdkException('protocol', 'Invalid websocket accept digest'); }
            return $connection;
        } catch (\Throwable $error) { $connection->close(); throw $error; }
    }

    public function send(string $method, object|array|null $parameters = null): object
    {
        return $this->sendScoped($method, $parameters, null);
    }
    public function session(string $id): ProtocolTransport
    {
        if ($id === '') { throw new \InvalidArgumentException('Session ID is required'); }
        return new class($this, $id) implements ProtocolTransport {
            public function __construct(private ProtocolConnection $connection, private string $id) {}
            public function send(string $method, object|array|null $parameters = null): object { return $this->connection->sendScoped($method, $parameters, $this->id); }
        };
    }
    public function sendScoped(string $method, object|array|null $parameters, ?string $sessionId): object
    {
        if ($this->closed) { throw new SdkException('closed', 'CDP connection is closed'); }
        if ($method === '') { throw new \InvalidArgumentException('Method is required'); }
        $id = ++$this->sequence;
        $request = ['id' => $id, 'method' => $method, 'params' => $parameters === null ? new \stdClass() : (object) $parameters];
        if ($parameters instanceof \JsonSerializable) { $request['params'] = $parameters; }
        if ($sessionId !== null) { $request['sessionId'] = $sessionId; }
        $this->writeFrame(1, json_encode($request, JSON_THROW_ON_ERROR | JSON_PRESERVE_ZERO_FRACTION));
        $deadline = microtime(true) + $this->timeout;
        while (!isset($this->responses[$id])) {
            $remaining = $deadline - microtime(true);
            if ($remaining <= 0) { throw new SdkException('timeout', 'CDP command timed out: ' . $method); }
            stream_set_timeout($this->socket, (int) $remaining, (int) (($remaining - (int) $remaining) * 1_000_000));
            $message = json_decode($this->readMessage(), false, 512, JSON_THROW_ON_ERROR);
            if (!is_object($message)) { throw new SdkException('protocol', 'CDP message must be an object'); }
            if (isset($message->id)) {
                // This synchronous transport has exactly one active call; drop late/foreign replies.
                if ($message->id === $id && ($message->sessionId ?? null) === $sessionId) { $this->responses[$id] = $message; }
            }
            else { $this->events[] = $message; if (count($this->events) > 1000) { array_shift($this->events); } }
        }
        $response = $this->responses[$id]; unset($this->responses[$id]);
        if (isset($response->error)) {
            throw new ProtocolException($response->error->code, $response->error->message, $response->error->data ?? null, property_exists($response->error, 'data'));
        }
        return $response->result ?? new \stdClass();
    }
    public function drainEvents(): array { $events = $this->events; $this->events = []; return $events; }
    private function writeAll(string $data): void
    {
        while ($data !== '') {
            $written = fwrite($this->socket, $data);
            if ($written === false || $written === 0) { throw new SdkException('connection', 'CDP write failed'); }
            $data = substr($data, $written);
        }
    }
    private function writeFrame(int $opcode, string $payload): void
    {
        $length = strlen($payload);
        $header = chr(0x80 | $opcode);
        if ($length < 126) { $header .= chr(0x80 | $length); }
        elseif ($length <= 65535) { $header .= chr(0x80 | 126) . pack('n', $length); }
        else { $header .= chr(0x80 | 127) . pack('NN', 0, $length); }
        $mask = random_bytes(4);
        $masked = $payload ^ substr(str_repeat($mask, (int) ceil($length / 4)), 0, $length);
        $this->writeAll($header . $mask . $masked);
    }
    private function readExact(int $length): string
    {
        $result = '';
        while (strlen($result) < $length) {
            $part = fread($this->socket, $length - strlen($result));
            if ($part === false || $part === '') {
                $metadata = stream_get_meta_data($this->socket);
                throw new SdkException(($metadata['timed_out'] ?? false) ? 'timeout' : 'closed', 'CDP socket ended while reading a frame');
            }
            $result .= $part;
        }
        return $result;
    }
    private function readMessage(): string
    {
        $message = '';
        $started = false;
        while (true) {
            $header = $this->readExact(2); $first = ord($header[0]); $second = ord($header[1]);
            if (($first & 0x70) !== 0 || ($second & 0x80) !== 0) { throw new SdkException('protocol', 'Unsupported websocket frame flags'); }
            $opcode = $first & 0x0f; $length = $second & 0x7f;
            if ($length === 126) { $length = unpack('n', $this->readExact(2))[1]; }
            elseif ($length === 127) { $parts = unpack('Nhigh/Nlow', $this->readExact(8)); if ($parts['high'] !== 0) { throw new SdkException('protocol', 'CDP frame too large'); } $length = $parts['low']; }
            if ($length > 64 * 1024 * 1024 || strlen($message) + $length > 64 * 1024 * 1024) { throw new SdkException('protocol', 'CDP message exceeds 64 MiB'); }
            $payload = $this->readExact($length);
            if ($opcode === 8) { $this->close(); throw new SdkException('closed', 'CDP connection closed'); }
            if ($opcode === 9) { $this->writeFrame(10, $payload); continue; }
            if ($opcode === 10) { continue; }
            if ($opcode === 1 && !$started) { $started = true; }
            elseif ($opcode !== 0 || !$started) { throw new SdkException('protocol', 'Expected text CDP message'); }
            $message .= $payload;
            if (($first & 0x80) !== 0) { return $message; }
        }
    }
    public function close(): void
    {
        if ($this->closed) { return; }
        $this->closed = true;
        if (is_resource($this->socket)) { fclose($this->socket); }
        $this->responses = [];
    }
    public function __destruct() { $this->close(); }
}
