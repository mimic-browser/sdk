<?php
declare(strict_types=1);
require __DIR__ . '/unit.php';

use Mimic\Sdk\Chrome\ChromeSession;
use Mimic\Sdk\ContextSetup;
use Mimic\Sdk\Generated\GetMediaSourcesParams;
use Mimic\Sdk\Generated\MediaConfiguration;

check(PHP_OS_FAMILY === 'Linux', 'Synthetic media tests are Linux-only');
$fixture = $argv[2];
$state = fn(): object => json_decode(file_get_contents($fixture . '/state'), false, 512, JSON_THROW_ON_ERROR);
$before = $state(); $privateSource = null;
$session = ChromeSession::connect($argv[1]);
try {
    $context = $session->newContext(['profile' => ['generate' => ['seed' => 'php-media-environment']]], function (ContextSetup $setup) use (&$privateSource): MediaConfiguration {
        $query = new GetMediaSourcesParams(); $query->browserContextId = $setup->browserContextId;
        $sources = $setup->mimic->commands->getMediaSources($query)->sources;
        $camera = array_values(array_filter($sources, fn($source) => $source->label === 'Private native camera B'))[0];
        $microphone = array_values(array_filter($sources, fn($source) => $source->label === 'Private native microphone B'))[0];
        $privateSource = $camera->sourceId;
        return MediaConfiguration::fromWire(['devices' => [
            ['key' => 'front', 'kind' => 'videoinput', 'source' => ['sourceId' => $camera->sourceId], 'label' => 'Studio Camera', 'group' => 'desk', 'modes' => [['width' => 16, 'height' => 8, 'frameRate' => 30]], 'defaultMode' => ['width' => 16, 'height' => 8, 'frameRate' => 30], 'processing' => ['resize' => 'crop-and-scale']],
            ['key' => 'voice', 'kind' => 'audioinput', 'source' => ['sourceId' => $microphone->sourceId], 'label' => 'Studio Microphone', 'group' => 'desk'],
        ]]);
    });
    check($state() == $before, 'Discovery/configuration opened capture');
    $session->mimic->send('Browser.grantPermissions', ['browserContextId' => $context->id, 'origin' => $fixture, 'permissions' => ['videoCapture', 'audioCapture']]);
    $page = $session->newPage($context); $page->navigate($fixture)->waitForNavigation();
    $page->mouse()->move(1, 1)->click();
    $script = rtrim(trim(file_get_contents(__DIR__ . '/../../dotnet/Mimic.Tests/media_capture.js')), ';');
    $observed = $page->evaluate('(' . $script . ')()')->getReturnValue(15000);
    check($observed['pixel'] === [0, 0, 255, 255], 'Private B camera did not deliver blue pixels');
    check($observed['audioEnergy']['b'] > 1 && $observed['audioEnergy']['b'] > 5 * $observed['audioEnergy']['a'], 'Private B microphone PCM did not reach Web Audio');
    check(!str_contains(json_encode($observed), 'Private native') && !str_contains(json_encode($observed), $privateSource), 'Private identity leaked to page');
    check(count($observed['devices']) === 2 && $observed['devices'][0]['groupId'] === $observed['devices'][1]['groupId'], 'Public media grouping changed');
} finally { $session->close(); }
for ($attempt = 0; $attempt < 100; $attempt++) {
    $last = $state(); $closed = true;
    foreach (get_object_vars($last->opens) as $id => $count) { if (($last->closes->$id ?? 0) !== $count) { $closed = false; } }
    if ($closed) { break; } usleep(20000);
}
foreach (['native-camera-b', 'native-microphone-b'] as $source) {
    $expected = ($before->opens->$source ?? 0) + 1;
    check(($last->opens->$source ?? 0) === $expected && ($last->closes->$source ?? 0) === $expected, 'Private capture worker leaked or wrong source opened');
}
check(($last->opens->{'native-camera-a'} ?? 0) === ($before->opens->{'native-camera-a'} ?? 0), 'Capture silently selected source A');
echo "PASS PHP Chrome public identities, private B blue frames/660 Hz PCM, capture cleanup\n";
