<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final class RuntimeOptions
{
    public function __construct(
        public ?string $version = null,
        public ?string $lockFile = null,
        public ?string $executablePath = null,
        public ?string $runtimeDirectory = null,
        public bool $allowDownload = true,
        public float $timeout = 60.0,
        public float $lockTimeout = 120.0,
        public array $arguments = [],
        public ?string $archivePath = null,
    ) {}
}
