<?php
declare(strict_types=1);
namespace Mimic\Sdk;

/** Explicit Context identity, without a guessed current page or injected scope. */
final readonly class ContextSetup
{
    public function __construct(public string $browserContextId, public MimicClient $mimic) {}
}
