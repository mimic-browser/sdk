package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;

public final class MimicContext {
    private final ProtocolTransport transport;
    private final String id;
    public MimicContext(ProtocolTransport transport, String id) {
        if (id == null || id.isEmpty()) throw new IllegalArgumentException("Context ID is required");
        this.transport = transport;
        this.id = id;
    }
    public String id() { return id; }
    public JsonObject send(String method, JsonObject parameters) {
        var arguments = parameters == null ? new JsonObject() : parameters.deepCopy();
        if (arguments.has("browserContextId") && !arguments.get("browserContextId").getAsString().equals(id)) throw new IllegalArgumentException("Context ID conflicts with bound Context");
        arguments.addProperty("browserContextId", id);
        return transport.send(method, arguments);
    }
    public JsonObject getMediaProfile() { return send("Mimic.getMediaProfile", null); }
    public JsonObject configure(JsonObject configuration) { return send("Mimic.configureContext", configuration); }
    public JsonObject configure(ContextConfiguration configuration) { return configure(configuration.toWire()); }
    public JsonObject setMediaProfile(JsonObject profile) { return send("Mimic.setMediaProfile", profile); }
    public JsonObject setMediaProfile(Generated.MediaConfiguration profile) { return setMediaProfile(Generated.toWire(profile).getAsJsonObject()); }
    public JsonObject getResourcePolicy() { return send("Mimic.getResourcePolicy", null); }
    public JsonObject setResourcePolicy(JsonObject policy) {
        var parameters = new JsonObject(); parameters.add("policy", policy.deepCopy());
        return send("Mimic.updateResourcePolicy", parameters);
    }
    public JsonObject setResourcePolicy(Generated.ResourcePolicy policy) { return setResourcePolicy(Generated.toWire(policy).getAsJsonObject()); }
}
