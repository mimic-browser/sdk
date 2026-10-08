<?php
declare(strict_types=1);
namespace Mimic\Sdk\Chrome;

use HeadlessChromium\Browser;
use HeadlessChromium\BrowserFactory;
use HeadlessChromium\Communication\Message;
use HeadlessChromium\Page;
use Mimic\Sdk\MimicClient;
use Mimic\Sdk\MimicContext;
use Mimic\Sdk\ContextSetup;
use Mimic\Sdk\Generated\MediaConfiguration;
use Mimic\Sdk\RuntimeManager;
use Mimic\Sdk\RuntimeOptions;
use Mimic\Sdk\RuntimeProcess;

/** Optional chrome-php adapter; Browser and Page remain the real client classes. */
final class ChromeSession
{
    private bool $closed = false;
    private array $contexts = [];
    public readonly MimicClient $mimic;
    private function __construct(public readonly Browser $browser, public readonly RuntimeProcess $runtime)
    {
        $this->mimic = new MimicClient($runtime->transport);
    }
    public static function launch(?RuntimeOptions $options = null, array $clientOptions = []): self
    {
        return self::attach((new RuntimeManager())->launch($options), $clientOptions);
    }
    public static function connect(string $endpoint, array $clientOptions = []): self
    {
        return self::attach(RuntimeProcess::connect($endpoint), $clientOptions);
    }
    private static function attach(RuntimeProcess $runtime, array $options): self
    {
        try {
            if (!class_exists(BrowserFactory::class)) { throw new \LogicException('Install chrome-php/chrome ^1.16 to use the Chrome adapter'); }
            return new self(BrowserFactory::connectToBrowser($runtime->webSocketEndpoint, $options), $runtime);
        } catch (\Throwable $error) { $runtime->close(); throw $error; }
    }
    /** Chrome PHP has no native Context type; this capability handle owns an actual runtime Context. */
    public function newContext(object|array|null $configuration = null, ?callable $mediaFactory = null): MimicContext
    {
        if ($this->closed) { throw new \LogicException('Session is closed'); }
        $params = $configuration === null ? new \stdClass() : clone (object) $configuration;
        if (property_exists($params, 'media') && $mediaFactory !== null) { throw new \InvalidArgumentException('Select a media configuration or a media factory'); }
        foreach (array_keys(get_object_vars($params)) as $key) {
            if (!in_array($key, ['profile', 'proxy', 'resourcePolicy', 'media', 'disposeOnDetach'], true)) {
                throw new \InvalidArgumentException('Unknown Context configuration: ' . $key);
            }
        }
        $managed = property_exists($params, 'profile') || property_exists($params, 'proxy');
        $nativeOptions = new \stdClass();
        if (property_exists($params, 'disposeOnDetach')) { $nativeOptions->disposeOnDetach = $params->disposeOnDetach; }
        $result = $this->runtime->transport->send($managed ? 'Mimic.createContext' : 'Target.createBrowserContext', $managed ? $params : $nativeOptions);
        $id = $result->browserContextId;
        $this->contexts[$id] = true;
        try {
            $context = $this->mimic->context($id);
            if (!$managed) {
                if (property_exists($params, 'media')) { $context->setMediaProfile($params->media); }
                if (property_exists($params, 'resourcePolicy')) { $context->setResourcePolicy($params->resourcePolicy); }
            }
            if ($mediaFactory !== null) {
                $media = $mediaFactory(new ContextSetup($id, $this->mimic));
                if ($this->closed) { throw new \LogicException('Session closed during Context setup'); }
                if (!$media instanceof MediaConfiguration) { throw new \UnexpectedValueException('Media factory must return Generated\\MediaConfiguration'); }
                $context->setMediaProfile($media->jsonSerialize());
            }
            return $context;
        } catch (\Throwable $error) {
            unset($this->contexts[$id]);
            if (!$this->closed) { $this->runtime->transport->send('Target.disposeBrowserContext', ['browserContextId' => $id]); }
            throw $error;
        }
    }
    /** Public CDP target creation followed by public getPage returns a genuine Page. */
    public function newPage(MimicContext $context): Page
    {
        if (!isset($this->contexts[$context->id])) { throw new \InvalidArgumentException('Context is not owned by this integration'); }
        $response = $this->browser->getConnection()->sendMessageSync(new Message('Target.createTarget', ['url' => 'about:blank', 'browserContextId' => $context->id]));
        if (!$response->isSuccessful()) { throw new \RuntimeException('Native Chrome PHP target creation failed'); }
        $targetId = $response->getResultData('targetId');
        try { return $this->browser->getPage($targetId); }
        catch (\Throwable $error) { $this->runtime->transport->send('Target.closeTarget', ['targetId' => $targetId]); throw $error; }
    }
    public function forPage(Page $page): MimicContext
    {
        if ($page->getSession()->getConnection() !== $this->browser->getConnection()) { throw new \InvalidArgumentException('Page belongs to another browser'); }
        $target = $this->runtime->transport->send('Target.getTargetInfo', ['targetId' => $page->getSession()->getTargetId()]);
        return $this->mimic->context($target->targetInfo->browserContextId);
    }
    public function close(): void
    {
        if ($this->closed) { return; } $this->closed = true;
        $failure = null;
        try {
            foreach (array_keys($this->contexts) as $id) {
                try { $this->runtime->transport->send('Target.disposeBrowserContext', ['browserContextId' => $id]); }
                catch (\Throwable $error) { $failure ??= $error; }
            }
        } finally {
            // Browser::close sends global Browser.close, so attached sessions disconnect instead.
            try { $this->browser->getConnection()->disconnect(); }
            finally { $this->runtime->close(); }
        }
        if ($failure !== null) { throw $failure; }
    }
    public function __destruct() { $this->close(); }
}
