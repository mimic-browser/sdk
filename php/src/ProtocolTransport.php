<?php
declare(strict_types=1);
namespace Mimic\Sdk;

interface ProtocolTransport
{
    public function send(string $method, object|array|null $parameters = null): object;
}
