using System.Text.Json.Nodes;
using Mimic.Sdk.Generated;

namespace Mimic.Sdk;

/// <summary>Mimic settings applied to a native framework context before its first page.</summary>
public sealed class ContextConfiguration
{
    public Optional<JsonNode> Profile { get; init; }
    public Optional<Proxy> Proxy { get; init; }
    public Optional<MediaConfiguration> Media { get; init; }
    public Optional<ResourcePolicy> ResourcePolicy { get; init; }

    public JsonObject ToWire() => Wire.Encode(new CreateContextParams
    {
        Profile = Profile, Proxy = Proxy, Media = Media, ResourcePolicy = ResourcePolicy
    });
}
