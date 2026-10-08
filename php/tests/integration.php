<?php
declare(strict_types=1);
require __DIR__ . '/unit.php';

use Mimic\Sdk\Chrome\ChromeSession;
use Mimic\Sdk\Generated\GenerateProfileParams;
use Mimic\Sdk\ProtocolException;
use Mimic\Sdk\RuntimeManager;
use Mimic\Sdk\RuntimeOptions;

check(PHP_OS_FAMILY === 'Linux', 'Integration tests must run inside Linux; no Windows listeners');
if (($argv[1] ?? '') === '--install-offline') {
    $installed = (new RuntimeManager())->install(new RuntimeOptions(runtimeDirectory: $argv[2], allowDownload: false, archivePath: $argv[3]));
    RuntimeManager::verify($installed->directory, $installed->runtimeLock, 'linux-amd64');
    echo "PASS PHP verified offline archive/shared installation\n"; exit(0);
}
if (($argv[1] ?? '') === '--install') {
    $root = $argv[2]; $manager = new RuntimeManager(); $options = new RuntimeOptions(runtimeDirectory: $root);
    $installation = $manager->install($options); $cached = $manager->install(new RuntimeOptions(runtimeDirectory: $root, allowDownload: false));
    check($installation->executablePath === $cached->executablePath, 'Offline cache reuse');
    $runtime = $manager->launch(new RuntimeOptions(runtimeDirectory: $root, allowDownload: false));
    try { check($runtime->identity->version === $installation->release, 'Installed runtime identity'); fails(fn() => RuntimeManager::prune($installation->directory, $options), 'lease'); }
    finally { $runtime->close(); }
    echo "PASS PHP verified install, shared cross-language cache, offline launch and lease guard\n"; exit(0);
}
$session = ChromeSession::launch(new RuntimeOptions(executablePath: $argv[1], allowDownload: false));
$pid = $session->runtime->processId;
try {
    check($session->browser instanceof HeadlessChromium\Browser, 'Adapter returned imitation browser');
    $context = $session->newContext(['media' => ['devices' => []], 'resourcePolicy' => ['reportOnly' => true]]);
    check($context->getMediaProfile()->profile->devices === [], 'Media profile');
    check($context->getResourcePolicy()->policy->reportOnly === true, 'Resource policy');
    $page = $session->newPage($context);
    $page->setHtml('<title>PHP native</title><button id="run" onclick="this.textContent=\'done\'">run</button>');
    $page->dom()->querySelector('#run')->click();
    check($page->evaluate('document.querySelector("#run").textContent')->getReturnValue() === 'done', 'Native Chrome PHP click scenario');
    check($session->forPage($page)->id === $context->id, 'Public page-to-Context bridge');
    check($session->mimic->commands->getVersion()->version === $session->runtime->identity->version, 'Typed response');
    try { $session->mimic->experimental()->send('Mimic.futureUnsupported', ['null' => null]); throw new RuntimeException('Unknown command accepted'); }
    catch (ProtocolException $error) { check($error->getCode() < 0 && $error->getMessage() !== '', 'Raw error lost code/message'); }
    $invalid = new GenerateProfileParams(); $invalid->seed = null;
    try { $session->mimic->commands->generateProfile($invalid); throw new RuntimeException('Invalid null accepted'); }
    catch (ProtocolException $error) { check($error->getCode() === -32602 && $error->hasData, 'Typed error lost structured data'); }
    $attached = ChromeSession::connect($session->runtime->endpoint);
    try { $owned = $attached->newContext(); $attached->newPage($owned); }
    finally { $attached->close(); }
    check($page->evaluate('document.title')->getReturnValue() === 'PHP native', 'Attached disposal affected other client');
    $managed = $session->newContext(['profile' => ['generate' => ['platform' => 'windows', 'seed' => 'php-sdk-profile']]]);
    check($session->newPage($managed)->evaluate('navigator.platform')->getReturnValue() === 'Win32', 'Managed profile did not reach native page');
    $factoryCalled = false;
    $factoryContext = $session->newContext(['profile' => ['generate' => ['platform' => 'windows', 'seed' => 'php-media-factory']]], function (\Mimic\Sdk\ContextSetup $setup) use (&$factoryCalled): \Mimic\Sdk\Generated\MediaConfiguration {
        $query = new \Mimic\Sdk\Generated\GetMediaSourcesParams(); $query->browserContextId = $setup->browserContextId;
        check(is_array($setup->mimic->commands->getMediaSources($query)->sources), 'Typed Context source discovery');
        $factoryCalled = true;
        $media = new \Mimic\Sdk\Generated\MediaConfiguration(); $media->devices = []; return $media;
    });
    check($factoryCalled && $session->newPage($factoryContext)->evaluate('navigator.platform')->getReturnValue() === 'Win32', 'Media factory/profile setup');
    $ordinary = $session->newContext(null, function (\Mimic\Sdk\ContextSetup $setup): \Mimic\Sdk\Generated\MediaConfiguration {
        $media = new \Mimic\Sdk\Generated\MediaConfiguration(); $media->devices = []; return $media;
    });
    $ordinaryPage = $session->newPage($ordinary);
    $emulation = $ordinaryPage->getSession()->sendMessageSync(new \HeadlessChromium\Communication\Message('Emulation.setUserAgentOverride', ['userAgent' => 'Ordinary PHP Context']));
    check($emulation->isSuccessful() && $ordinaryPage->evaluate('navigator.userAgent')->getReturnValue() === 'Ordinary PHP Context', 'Factory-only setup silently managed the environment');
    $before = $session->mimic->send('Target.getBrowserContexts')->browserContextIds;
    try { $session->newContext(null, function () { throw new RuntimeException('factory failure'); }); throw new RuntimeException('Factory error swallowed'); }
    catch (RuntimeException $error) { check($error->getMessage() === 'factory failure', 'Factory error changed'); }
    $after = $session->mimic->send('Target.getBrowserContexts')->browserContextIds; sort($before); sort($after);
    check($after === $before, 'Failed media factory leaked Context');
    $callbackSession = ChromeSession::connect($session->runtime->endpoint);
    try {
        $callbackSession->newContext(null, function () use ($callbackSession): \Mimic\Sdk\Generated\MediaConfiguration {
            $callbackSession->close(); $empty = new \Mimic\Sdk\Generated\MediaConfiguration(); $empty->devices = []; return $empty;
        });
        throw new RuntimeException('Closed callback session returned Context');
    } catch (LogicException $error) { check(str_contains($error->getMessage(), 'closed'), 'Callback shutdown error changed'); }
    finally { $callbackSession->close(); }
    $after = $session->mimic->send('Target.getBrowserContexts')->browserContextIds; sort($after);
    check($after === $before, 'Closing factory leaked Context');
} finally { $session->close(); }
check(!is_dir('/proc/' . $pid), 'Owned process survived disposal');
$temporary = sys_get_temp_dir() . '/mimic-php-timeout-' . RuntimeManager::uuid(); RuntimeManager::mkdir($temporary);
try {
    $fake = "$temporary/runtime";
    file_put_contents($fake, '#!/bin/sh' . "\n" . 'echo $$ > "' . $temporary . '/pid"' . "\nexec sleep 60\n"); chmod($fake, 0700);
    fails(fn() => (new RuntimeManager())->launch(new RuntimeOptions(executablePath: $fake, allowDownload: false, timeout: 0.2)), 'startup');
    $child = trim(file_get_contents("$temporary/pid")); check(!is_dir('/proc/' . $child), 'Timed out runtime survived cleanup');
} finally { RuntimeManager::removeTree($temporary); }
echo "PASS PHP genuine chrome-php/chrome, Context bridge, raw/typed errors, attach isolation, owned teardown\n";
