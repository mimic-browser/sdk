<?php
declare(strict_types=1);
require dirname(__DIR__, 2) . '/php/vendor/autoload.php';

use Mimic\Sdk\Chrome\ChromeSession;
use Mimic\Sdk\RuntimeOptions;

$session = ChromeSession::launch(new RuntimeOptions(executablePath: $argv[1] ?? null));
try {
    $context = $session->newContext();
    $page = $session->newPage($context);
    $page->setHtml('<h1>Hello from the native PHP client</h1>');
    echo $page->evaluate('document.querySelector("h1").textContent')->getReturnValue(), PHP_EOL;
    echo 'Mimic ', $session->mimic->commands->getVersion()->version, PHP_EOL;
} finally {
    $session->close();
}
