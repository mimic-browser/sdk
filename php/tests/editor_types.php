<?php
declare(strict_types=1);

use HeadlessChromium\Browser;
use HeadlessChromium\Page;
use Mimic\Sdk\Chrome\ChromeSession;
use Mimic\Sdk\Generated\CameraFormat;
use Mimic\Sdk\Generated\GetMediaSourcesResult;
use Mimic\Sdk\Generated\MediaConfiguration;
use Mimic\Sdk\Generated\MediaDeviceProfile;
use Mimic\Sdk\Generated\MediaSource;
use Mimic\Sdk\MimicContext;

// Static consumer fixture: analyze against the actual installed package.
// Functions are never called and never launch a browser.
function consumeSource(MediaSource $source): void {}
function consumeDevice(MediaDeviceProfile $device): void {}
function consumeMode(CameraFormat $mode): void {}
function consumeBrowser(Browser $browser): void {}
function consumePage(Page $page): void {}

function typedCollections(GetMediaSourcesResult $sources, MediaConfiguration $media, MediaDeviceProfile $device): void
{
    if (is_array($sources->sources)) {
        foreach ($sources->sources as $source) { consumeSource($source); }
    }
    if (is_array($media->devices)) {
        foreach ($media->devices as $entry) { consumeDevice($entry); }
    }
    if (is_array($device->modes)) {
        foreach ($device->modes as $mode) { consumeMode($mode); }
    }
}

function nativeObjects(ChromeSession $session, MimicContext $context): void
{
    consumeBrowser($session->browser);
    consumePage($session->newPage($context));
    $capability = $session->forPageCommands($session->newPage($context));
    $capability->commands->getMediaSources();
}
