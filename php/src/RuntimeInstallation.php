<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final readonly class RuntimeInstallation
{
    public function __construct(public string $executablePath, public string $release, public ?string $directory, public object $runtimeLock) {}
}
