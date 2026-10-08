package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;

public final class MimicClient implements ProtocolTransport {
    private final ProtocolTransport transport;
    private final Generated.MimicCommands commands;
    public MimicClient(ProtocolTransport transport) { this.transport = transport; commands = new Generated.MimicCommands(transport); }
    public Generated.MimicCommands commands() { return commands; }
    public ProtocolTransport experimental() { return transport; }
    @Override public JsonObject send(String method, JsonObject parameters) { return transport.send(method, parameters); }
    public MimicContext context(String id) { return new MimicContext(transport, id); }
}
