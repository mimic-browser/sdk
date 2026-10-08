<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final class SdkException extends \RuntimeException
{
    public function __construct(public readonly string $kind, string $message, ?\Throwable $previous = null)
    {
        parent::__construct($message, 0, $previous);
    }
}
