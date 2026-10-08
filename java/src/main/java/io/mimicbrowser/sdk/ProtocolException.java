package io.mimicbrowser.sdk;

import com.google.gson.JsonElement;

public final class ProtocolException extends RuntimeException {
    private final int code;
    private final JsonElement data;
    public ProtocolException(int code, String message, JsonElement data) {
        super(message);
        this.code = code;
        this.data = data == null ? null : data.deepCopy();
    }
    public int code() { return code; }
    public JsonElement data() { return data == null ? null : data.deepCopy(); }
}
