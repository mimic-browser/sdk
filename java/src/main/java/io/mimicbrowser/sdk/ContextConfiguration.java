package io.mimicbrowser.sdk;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

/** Mimic settings applied to a native framework context before its first page. */
public final class ContextConfiguration {
    public Generated.OptionalValue<JsonElement> profile;
    public Generated.OptionalValue<Generated.Proxy> proxy;
    public Generated.OptionalValue<Generated.MediaConfiguration> media;
    public Generated.OptionalValue<Generated.ResourcePolicy> resourcePolicy;

    public JsonObject toWire() {
        var parameters = new Generated.CreateContextParams();
        parameters.profile = profile;
        parameters.proxy = proxy;
        parameters.media = media;
        parameters.resourcePolicy = resourcePolicy;
        return Generated.toWire(parameters).getAsJsonObject();
    }
}
