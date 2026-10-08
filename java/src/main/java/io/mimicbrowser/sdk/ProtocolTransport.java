package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;

/** An explicitly bound CDP transport shared by typed and experimental calls. */
@FunctionalInterface
public interface ProtocolTransport {
    JsonObject send(String method, JsonObject parameters);
}
