<?php
declare(strict_types=1);
require dirname(__DIR__) . '/vendor/autoload.php';

use Mimic\Sdk\Generated\Missing;
use Mimic\Sdk\MimicClient;
use Mimic\Sdk\ProtocolTransport;
use Mimic\Sdk\RuntimeManager;
use Mimic\Sdk\RuntimeOptions;
use Mimic\Sdk\SdkException;

function check(bool $condition, string $message): void { if (!$condition) { throw new RuntimeException($message); } }
function fails(callable $call, string $kind): void {
    try { $call(); } catch (SdkException $error) { check($error->kind === $kind, 'Expected ' . $kind . ', got ' . $error->kind); return; }
    throw new RuntimeException('Expected failure: ' . $kind);
}
function hydrate(string $class, object $wire): object {
    $model = new $class();
    foreach (get_object_vars($wire) as $key => $value) {
        $property = new ReflectionProperty($class, $key); $type = $property->getType();
        $types = $type instanceof ReflectionUnionType ? $type->getTypes() : [$type];
        foreach ($types as $candidate) {
            if (!$candidate->isBuiltin() && $candidate->getName() !== Missing::class && is_object($value)) { $value = hydrate($candidate->getName(), $value); break; }
            if ($candidate->getName() === 'array' && is_object($value)) { $value = get_object_vars($value); break; }
        }
        $model->$key = $value;
    }
    return $model;
}
$corpus = json_decode(file_get_contents(dirname(__DIR__, 2) . '/conformance/fixtures/wire.json'), false, 512, JSON_THROW_ON_ERROR);
$count = 0;
foreach ($corpus->cases as $case) {
    if (!$case->valid && $case->name !== 'explicit_null_is_not_omission') { continue; }
    $class = 'Mimic\\Sdk\\Generated\\' . $case->model;
    $model = $class::fromWire($case->wire);
    check(json_decode(json_encode($model, JSON_THROW_ON_ERROR), false, 512, JSON_THROW_ON_ERROR) == $case->wire, 'Wire round trip: ' . $case->name); $count++;
}
check(RuntimeManager::normalizeVersion('0.2.2') === 'v0.2.2', 'Version normalization');
foreach (['latest', '^0.2.2', 'v01.2.2', '0.2', '../../escape', '0.2.2+meta'] as $invalid) { fails(fn() => RuntimeManager::normalizeVersion($invalid), 'configuration'); }
$lock = RuntimeManager::defaultLock(); RuntimeManager::validateLock($lock);
$bad = clone $lock; $bad->manifestJson = '{}'; fails(fn() => RuntimeManager::validateLock($bad), 'integrity');
fails(fn() => RuntimeManager::validateLock($lock, 'v999.0.0'), 'configuration');
$temporary = sys_get_temp_dir() . '/mimic-php-unit-' . RuntimeManager::uuid(); RuntimeManager::mkdir($temporary);
try {
    $zip = new ZipArchive(); $archive = "$temporary/unsafe.zip"; $zip->open($archive, ZipArchive::CREATE); $zip->addFromString('../escape', 'bad'); $zip->close();
    fails(fn() => RuntimeManager::extractArchive($archive, "$temporary/tree", 'root', true), 'integrity');
    check(!is_file("$temporary/escape"), 'Archive escaped');
    $manager = new RuntimeManager();
    fails(fn() => $manager->install(new RuntimeOptions(runtimeDirectory: $temporary, allowDownload: false)), 'offline');
    $lockPath = "$temporary/lock.json"; RuntimeManager::writeJson($lockPath, $lock);
    fails(fn() => $manager->resolveLock(new RuntimeOptions(version: '999.0.0', lockFile: $lockPath, allowDownload: false)), 'configuration');
    file_put_contents("$temporary/mimic", 'fixture');
    $explicit = $manager->install(new RuntimeOptions(version: '0.9.9', executablePath: "$temporary/mimic", allowDownload: false));
    check($explicit->release === 'v0.9.9', 'Explicit executable resolved a release online');
    fails(fn() => $manager->install(new RuntimeOptions(lockFile: $lockPath, executablePath: "$temporary/mimic", allowDownload: false)), 'integrity');
    $capture = new class implements ProtocolTransport {
        public array $calls = [];
        public function send(string $method, object|array|null $parameters = null): object { $this->calls[] = [$method, $parameters]; return new stdClass(); }
    };
    $client = new MimicClient($capture); $raw = (object) ['unknown' => null, 'large' => 9007199254740991, 'values' => [false, 0, null]];
    $client->experimental()->send('Mimic.futureCommand', $raw); check($capture->calls[0] === ['Mimic.futureCommand', $raw], 'Experimental altered JSON');
    $client->context('context-x')->setMediaProfile(['devices' => []]);
    check($capture->calls[1][0] === 'Mimic.setMediaProfile' && $capture->calls[1][1]->browserContextId === 'context-x', 'Context media scope');
    $client->context('context-x')->setResourcePolicy([]);
    check($capture->calls[2][0] === 'Mimic.updateResourcePolicy' && is_object($capture->calls[2][1]->policy), 'Resource policy scope');
} finally { RuntimeManager::removeTree($temporary); }
echo "PASS PHP offline unit checks and $count shared wire round trips\n";
