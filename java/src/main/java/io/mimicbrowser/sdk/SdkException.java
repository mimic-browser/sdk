package io.mimicbrowser.sdk;

public final class SdkException extends RuntimeException {
    private final String kind;
    public SdkException(String kind, String message) { super(message); this.kind = kind; }
    public SdkException(String kind, String message, Throwable cause) { super(message, cause); this.kind = kind; }
    public String kind() { return kind; }
}
