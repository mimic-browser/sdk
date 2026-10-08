using System.Text.Json.Nodes;
using Mimic.Sdk.Generated;

namespace Mimic.Sdk;

/// <summary>Mimic capabilities accompanying genuine framework browser objects.</summary>
public sealed class MimicClient(IProtocolTransport transport) : IProtocolTransport
{
    public MimicCommands Commands { get; } = new(transport);
    public IProtocolTransport Experimental => transport;
    public Task<JsonObject> SendAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
        => transport.SendAsync(method, parameters, cancellationToken);
    public MimicContext Context(string browserContextId) => new(transport, browserContextId);
}

public sealed class MimicContext(IProtocolTransport transport, string id)
{
    public string Id { get; } = id;
    public Task<JsonObject> ConfigureAsync(JsonObject configuration, CancellationToken cancellationToken = default)
        => CallAsync("Mimic.configureContext", configuration, cancellationToken);
    public Task<JsonObject> GetMediaProfileAsync(CancellationToken cancellationToken = default)
        => CallAsync("Mimic.getMediaProfile", null, cancellationToken);
    public Task<JsonObject> SetMediaProfileAsync(JsonObject media, CancellationToken cancellationToken = default)
        => CallAsync("Mimic.setMediaProfile", media, cancellationToken);
    public Task<JsonObject> GetResourcePolicyAsync(CancellationToken cancellationToken = default)
        => CallAsync("Mimic.getResourcePolicy", null, cancellationToken);
    public Task<JsonObject> SetResourcePolicyAsync(JsonObject policy, CancellationToken cancellationToken = default)
        => CallAsync("Mimic.updateResourcePolicy", new JsonObject { ["policy"] = policy.DeepClone() }, cancellationToken);
    public Task<JsonObject> CallAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
    {
        var arguments = parameters?.DeepClone().AsObject() ?? new JsonObject();
        if (arguments["browserContextId"] is { } context && context.GetValue<string>() != Id) throw new ArgumentException("Context ID conflicts with bound Context");
        arguments["browserContextId"] = Id;
        return transport.SendAsync(method, arguments, cancellationToken);
    }
}
