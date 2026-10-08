<?php
declare(strict_types=1);
namespace Mimic\Sdk;

final class MimicContext
{
    public function __construct(private readonly ProtocolTransport $transport, public readonly string $id)
    {
        if ($id === '') { throw new \InvalidArgumentException('Context ID is required'); }
    }
    public function send(string $method, object|array|null $parameters = null): object
    {
        if ($parameters instanceof Generated\ConfigureContextParams && $parameters->browserContextId === Generated\Missing::Value) {
            $parameters = clone $parameters;
            $parameters->browserContextId = $this->id;
        }
        $params = $parameters instanceof \JsonSerializable ? $parameters->jsonSerialize() : ($parameters === null ? new \stdClass() : (object) $parameters);
        $params = clone $params;
        if (isset($params->browserContextId) && $params->browserContextId !== $this->id) { throw new \InvalidArgumentException('Context ID conflicts with bound Context'); }
        $params->browserContextId = $this->id;
        return $this->transport->send($method, $params);
    }
    /** @param Generated\ConfigureContextParams|object|array $configuration */
    public function configure(object|array $configuration): object { return $this->send('Mimic.configureContext', $configuration); }
    public function getMediaProfile(): object { return $this->send('Mimic.getMediaProfile'); }
    /** @param Generated\MediaConfiguration|object|array $profile */
    public function setMediaProfile(object|array $profile): object { return $this->send('Mimic.setMediaProfile', $profile); }
    public function getResourcePolicy(): object { return $this->send('Mimic.getResourcePolicy'); }
    /** @param Generated\ResourcePolicy|object|array $policy */
    public function setResourcePolicy(object|array $policy): object { return $this->send('Mimic.updateResourcePolicy', ['policy' => $policy instanceof \JsonSerializable ? $policy : (object) $policy]); }
}
