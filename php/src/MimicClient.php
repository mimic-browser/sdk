<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final class MimicClient implements ProtocolTransport
{
    public readonly Generated\MimicCommands $commands;
    public function __construct(private readonly ProtocolTransport $transport)
    {
        $this->commands = new Generated\MimicCommands(fn(string $method, mixed $params): object => $transport->send($method, $params));
    }
    public function experimental(): ProtocolTransport { return $this->transport; }
    public function send(string $method, object|array|null $parameters = null): object { return $this->transport->send($method, $parameters); }
    public function context(string $id): MimicContext { return new MimicContext($this->transport, $id); }
}
