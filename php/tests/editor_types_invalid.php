<?php
declare(strict_types=1);

use Mimic\Sdk\Generated\MediaSource;
use Mimic\Sdk\RuntimeOptions;

// Intentionally invalid static consumer: a type checker must reject both
// the misspelled field and incorrect parameter type without running it.
function rejectEditorTypos(MediaSource $source): void
{
    echo $source->soruceId;
    new RuntimeOptions(version: 42);
    new RuntimeOptions(versoin: '0.2.4');
}
