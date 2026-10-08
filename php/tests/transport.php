<?php
declare(strict_types=1);
require __DIR__ . '/unit.php';

use Mimic\Sdk\ProtocolConnection;
use Mimic\Sdk\ProtocolException;

check(PHP_OS_FAMILY === 'Linux', 'Transport fixture must run inside Linux');
$connection = ProtocolConnection::connect($argv[1]);
try {
    $input = json_decode('{"future":null,"values":[false,9007199254740991]}', false, 512, JSON_THROW_ON_ERROR);
    $result = $connection->session('owned-session')->send('Fixture.session', $input);
    check($result->owner === 'owned-session' && $result->echo == $input, 'Foreign session reply routed to request');
    try { $connection->send('Fixture.error'); throw new RuntimeException('Expected protocol error'); }
    catch (ProtocolException $error) {
        check($error->getCode() === -32123 && $error->getMessage() === 'precise native error' && is_array($error->data) && count($error->data) === 3 && $error->data[0] === null, 'Raw arbitrary error data lost');
    }
    $connection->timeout = 0.1;
    fails(fn() => $connection->send('Fixture.timeout'), 'timeout');
} finally { $connection->close(); }
echo "PASS PHP raw foreign-session rejection, exact JSON/error data and timeout\n";
