package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

/** Multiplexed, bounded, cancellable raw CDP; closing it never closes the browser. */
public final class ProtocolConnection implements ProtocolTransport, AutoCloseable, WebSocket.Listener {
    private record Pending(String sessionId, CompletableFuture<JsonObject> completion) {}
    private final ConcurrentHashMap<Long, Pending> pending = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong();
    private final AtomicBoolean closed = new AtomicBoolean();
    private final StringBuilder message = new StringBuilder();
    private volatile WebSocket socket;
    private volatile Duration timeout = Duration.ofSeconds(30);
    private volatile Consumer<JsonObject> events = ignored -> {};

    public static ProtocolConnection connect(URI endpoint) {
        return connect(endpoint, Duration.ofSeconds(20));
    }
    public static ProtocolConnection connect(URI endpoint, Duration timeout) {
        var connection = new ProtocolConnection();
        try {
            connection.socket = HttpClient.newBuilder().connectTimeout(timeout).build()
                .newWebSocketBuilder().connectTimeout(timeout).buildAsync(endpoint, connection).get(Math.max(1, timeout.toMillis()), TimeUnit.MILLISECONDS);
            return connection;
        } catch (Exception error) {
            connection.close();
            if (error instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SdkException(error instanceof InterruptedException ? "cancelled" : "connection", "Cannot connect to CDP endpoint", error);
        }
    }
    public void timeout(Duration value) {
        if (value.isZero() || value.isNegative()) throw new IllegalArgumentException("Timeout must be positive");
        timeout = value;
    }
    public void onEvent(Consumer<JsonObject> handler) { events = handler; }
    @Override public void onOpen(WebSocket websocket) {
        socket = websocket;
        if (closed.get()) websocket.abort(); else websocket.request(1);
    }
    public ProtocolTransport session(String sessionId) {
        if (sessionId == null || sessionId.isEmpty()) throw new IllegalArgumentException("Session ID is required");
        return (method, parameters) -> send(method, parameters, sessionId);
    }
    @Override public JsonObject send(String method, JsonObject parameters) { return send(method, parameters, null); }
    private JsonObject send(String method, JsonObject parameters, String sessionId) {
        if (closed.get()) throw new SdkException("closed", "CDP connection is closed");
        if (method == null || method.isEmpty()) throw new IllegalArgumentException("Method is required");
        long id = sequence.incrementAndGet();
        var response = new CompletableFuture<JsonObject>();
        pending.put(id, new Pending(sessionId, response));
        try {
            var request = new JsonObject();
            request.addProperty("id", id);
            request.addProperty("method", method);
            request.add("params", parameters == null ? new JsonObject() : parameters.deepCopy());
            if (sessionId != null) request.addProperty("sessionId", sessionId);
            synchronized (this) { socket.sendText(request.toString(), true).get(timeout.toMillis(), TimeUnit.MILLISECONDS); }
            return response.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new SdkException("cancelled", "CDP request interrupted", error);
        } catch (TimeoutException error) { throw new SdkException("timeout", "CDP command timed out: " + method, error);
        } catch (ExecutionException error) {
            if (error.getCause() instanceof RuntimeException nativeError) throw nativeError;
            throw new SdkException("connection", "CDP command failed", error.getCause());
        } finally { pending.remove(id); }
    }
    @Override public CompletionStage<?> onText(WebSocket websocket, CharSequence data, boolean last) {
        try {
            message.append(data);
            if (message.length() > 64 * 1024 * 1024) throw new SdkException("protocol", "CDP message exceeds 64 MiB");
            if (last) {
                var body = JsonParser.parseString(message.toString()).getAsJsonObject();
                message.setLength(0);
                if (body.has("id")) {
                    var id = body.get("id").getAsLong();
                    var request = pending.get(id);
                    var session = body.has("sessionId") && !body.get("sessionId").isJsonNull() ? body.get("sessionId").getAsString() : null;
                    if (request != null && java.util.Objects.equals(session, request.sessionId()) && pending.remove(id, request)) {
                        var completion = request.completion();
                        if (body.has("error")) {
                            var error = body.getAsJsonObject("error");
                            completion.completeExceptionally(new ProtocolException(error.get("code").getAsInt(), error.get("message").getAsString(), error.get("data")));
                        } else completion.complete(body.has("result") ? body.getAsJsonObject("result") : new JsonObject());
                    }
                } else CompletableFuture.runAsync(() -> events.accept(body));
            }
            websocket.request(1);
        } catch (Exception error) { fail(error); websocket.abort(); }
        return CompletableFuture.completedFuture(null);
    }
    @Override public CompletionStage<?> onClose(WebSocket websocket, int status, String reason) {
        fail(new SdkException("closed", "CDP connection closed: " + reason));
        return CompletableFuture.completedFuture(null);
    }
    @Override public void onError(WebSocket websocket, Throwable error) { fail(error); }
    private void fail(Throwable error) { closed.set(true); pending.values().forEach(value -> value.completion().completeExceptionally(error)); pending.clear(); }
    @Override public void close() { fail(new SdkException("closed", "CDP connection disposed")); if (socket != null) socket.abort(); }
}
