<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final class ProtocolException extends \RuntimeException
{
    public function __construct(int $code, string $message, public readonly mixed $data = null, public readonly bool $hasData = false)
    {
        parent::__construct($message, $code);
    }
}
