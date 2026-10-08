<?php
declare(strict_types=1);
namespace Mimic\Sdk\Chrome;

use Mimic\Sdk\Generated\MimicCommands;
use Mimic\Sdk\ProtocolConnection;
use Mimic\Sdk\ProtocolException;
use Mimic\Sdk\ProtocolTransport;

/** Typed Mimic page commands; closing detaches this handle without closing the native Page. */
final class PageClient implements ProtocolTransport
{
    private bool $closed = false;
    private ?\Closure $onClose;
    public readonly MimicCommands $commands;

    public function __construct(private readonly ProtocolConnection $connection, public readonly string $sessionId, callable $onClose)
    {
        $this->onClose = \Closure::fromCallable($onClose);
        $this->commands = new MimicCommands(fn(string $method, mixed $params): object => $this->send($method, $params));
    }

    public function isClosed(): bool { return $this->closed; }
    public function experimental(): ProtocolTransport { return $this; }

    public function send(string $method, object|array|null $parameters = null): object
    {
        if ($this->closed) { throw new \LogicException('Page capability attachment closed'); }
        return $this->connection->sendScoped($method, $parameters, $this->sessionId);
    }

    /** The native target has already detached all of its sessions. */
    public function invalidate(): void
    {
        if ($this->closed) { return; }
        $this->closed = true;
        $this->notifyClosed();
    }

    public function close(): void
    {
        if ($this->closed) { return; }
        $this->closed = true;
        try {
            $this->connection->send('Target.detachFromTarget', ['sessionId' => $this->sessionId]);
        } catch (ProtocolException $error) {
            // A native target close can win the race with explicit detach.
            if ($error->getCode() !== -32000 || $error->getMessage() !== 'No session with given id') { throw $error; }
        } finally { $this->notifyClosed(); }
    }

    private function notifyClosed(): void
    {
        $callback = $this->onClose;
        $this->onClose = null;
        if ($callback !== null) { $callback($this); }
    }
}
