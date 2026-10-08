package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/** Owned process or borrowed remote connection, never a global shared daemon. */
public final class RuntimeProcess implements AutoCloseable {
    private final Process process;
    private final ProtocolConnection connection;
    private final URI endpoint;
    private final URI websocket;
    private final JsonObject identity;
    private final Path lease;
    private final AtomicBoolean closed = new AtomicBoolean();
    private RuntimeProcess(Process process, ProtocolConnection connection, URI endpoint, URI websocket, JsonObject identity, Path lease) {
        this.process = process; this.connection = connection; this.endpoint = endpoint; this.websocket = websocket; this.identity = identity; this.lease = lease;
    }
    public URI endpoint() { return endpoint; }
    public URI webSocketEndpoint() { return websocket; }
    public JsonObject identity() { return identity.deepCopy(); }
    public boolean isOwned() { return process != null; }
    public Long processId() { return process == null ? null : process.pid(); }
    public ProtocolTransport transport() { return connection; }

    public static RuntimeProcess connect(String endpoint) {
        var uri = URI.create(endpoint);
        var websocket = discover(uri);
        var connection = ProtocolConnection.connect(websocket);
        try {
            var identity = connection.send("Mimic.getVersion", new JsonObject());
            validateIdentity(identity, null);
            return new RuntimeProcess(null, connection, uri, websocket, identity, null);
        } catch (RuntimeException error) { connection.close(); throw error; }
    }
    static RuntimeProcess start(RuntimeManager.Installation installation, RuntimeOptions options) {
        var arguments = new ArrayList<String>();
        arguments.add(installation.executablePath().toString());
        arguments.addAll(java.util.List.of("--browser-mode", "headless", "--listen", "127.0.0.1:0"));
        for (var argument : options.arguments) {
            if (argument.matches("--?(listen|browser-mode)(=.*)?")) throw new SdkException("configuration", "Arguments cannot override loopback binding or headless mode");
            arguments.add(argument);
        }
        Process child;
        try { child = new ProcessBuilder(arguments).start(); }
        catch (IOException error) { throw new SdkException("startup", "Cannot start Mimic", error); }
        var ready = new CompletableFuture<URI>();
        var diagnostics = new ArrayDeque<String>();
        var outputReader = drain(child.getInputStream(), true, ready, diagnostics);
        var errorReader = drain(child.getErrorStream(), false, ready, diagnostics);
        child.onExit().thenRun(() -> ready.completeExceptionally(new SdkException("startup", "Mimic exited before readiness (" + child.exitValue() + ")")));
        ProtocolConnection connection = null;
        Path lease = null;
        var deadline = System.nanoTime() + options.timeout.toNanos();
        try {
            var endpoint = ready.get(options.timeout.toMillis(), TimeUnit.MILLISECONDS);
            RuntimeManager.interrupted();
            var websocket = discover(endpoint, remaining(deadline));
            connection = ProtocolConnection.connect(websocket, remaining(deadline));
            connection.timeout(remaining(deadline));
            var identity = connection.send("Mimic.getVersion", new JsonObject());
            var enforce = installation.directory() != null || options.version != null || options.lockFile != null || System.getenv("MIMIC_RUNTIME_VERSION") != null;
            validateIdentity(identity, enforce ? installation.release() : null);
            if (installation.directory() != null) {
                var directory = installation.directory().resolve(".leases"); Files.createDirectories(directory);
                lease = directory.resolve(UUID.randomUUID() + ".json");
                var record = new JsonObject(); record.addProperty("launcherPid", ProcessHandle.current().pid()); record.addProperty("runtimePid", child.pid());
                record.addProperty("hostname", RuntimeManager.hostname()); record.addProperty("createdAt", Instant.now().toString());
                Files.writeString(lease, record.toString());
            }
            connection.timeout(Duration.ofSeconds(30));
            return new RuntimeProcess(child, connection, endpoint, websocket, identity, lease);
        } catch (Exception error) {
            if (connection != null) connection.close();
            child.destroyForcibly();
            boolean interrupted = Thread.interrupted();
            try { child.waitFor(); outputReader.join(1000); errorReader.join(1000); }
            catch (InterruptedException ignored) { interrupted = true; }
            if (lease != null) try { Files.deleteIfExists(lease); } catch (IOException ignored) { /* A stale lease prevents unsafe pruning. */ }
            if (error instanceof InterruptedException || interrupted) Thread.currentThread().interrupt();
            String output; synchronized (diagnostics) { output = String.join("\n", diagnostics); }
            throw new SdkException(error instanceof InterruptedException || interrupted || error instanceof SdkException sdk && sdk.kind().equals("cancelled") ? "cancelled" : "startup", "Mimic startup failed: " + error.getMessage() + "\n" + output, error);
        }
    }
    private static Thread drain(InputStream input, boolean stdout, CompletableFuture<URI> ready, ArrayDeque<String> diagnostics) {
        var worker = new Thread(() -> {
            try (var reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8))) {
                String line;
                var pattern = Pattern.compile("^Mimic listening on (http://127\\.0\\.0\\.1:[1-9]\\d*)$");
                while ((line = reader.readLine()) != null) {
                    synchronized (diagnostics) { diagnostics.addLast(line.substring(0, Math.min(line.length(), 2048))); while (diagnostics.size() > 30) diagnostics.removeFirst(); }
                    var match = pattern.matcher(line);
                    if (stdout && match.matches()) ready.complete(URI.create(match.group(1)));
                }
            } catch (IOException error) { ready.completeExceptionally(error); }
        }, "mimic-output");
        worker.setDaemon(true); worker.start(); return worker;
    }
    private static URI discover(URI endpoint) {
        return discover(endpoint, Duration.ofSeconds(15));
    }
    private static Duration remaining(long deadline) {
        var nanos = deadline - System.nanoTime();
        if (nanos <= 0) throw new SdkException("timeout", "Mimic startup timed out");
        return Duration.ofNanos(nanos);
    }
    private static URI discover(URI endpoint, Duration timeout) {
        if ("ws".equals(endpoint.getScheme()) || "wss".equals(endpoint.getScheme())) return endpoint;
        if (!"http".equals(endpoint.getScheme()) && !"https".equals(endpoint.getScheme())) throw new SdkException("configuration", "Endpoint must use HTTP(S) or WS(S)");
        try {
            var response = HttpClient.newBuilder().connectTimeout(timeout).build().send(
                HttpRequest.newBuilder(endpoint.resolve("/json/version")).timeout(timeout).GET().build(), HttpResponse.BodyHandlers.ofByteArray());
            if (response.statusCode() != 200) throw new SdkException("identity", "Discovery returned HTTP " + response.statusCode());
            var document = RuntimeManager.parse(response.body());
            var websocket = URI.create(RuntimeManager.string(document, "webSocketDebuggerUrl"));
            if (!java.util.List.of("ws", "wss").contains(websocket.getScheme()) || !websocket.getHost().equalsIgnoreCase(endpoint.getHost())) throw new SdkException("identity", "Discovery returned unrelated websocket endpoint");
            return websocket;
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new SdkException("cancelled", "Discovery interrupted", error);
        } catch (IOException error) { throw new SdkException("connection", "Cannot discover Mimic endpoint", error); }
    }
    private static void validateIdentity(JsonObject identity, String expected) {
        var version = RuntimeManager.string(identity, "version");
        if (version.isEmpty() || !identity.has("chromeVersion") || !identity.has("baseProfile")) throw new SdkException("identity", "Endpoint is not a compatible Mimic runtime");
        if (expected != null && !version.equals(expected) && !("v" + version).equals(expected)) throw new SdkException("identity", "Expected Mimic " + expected + ", received " + version);
    }
    @Override public void close() {
        if (!closed.compareAndSet(false, true)) return;
        boolean interrupted = Thread.interrupted();
        try {
            if (process != null && process.isAlive()) {
                connection.timeout(Duration.ofSeconds(3));
                try { connection.send("Browser.close", new JsonObject()); } catch (RuntimeException ignored) { /* Shutdown may close the socket before its response. */ }
                try { if (!process.waitFor(3, TimeUnit.SECONDS)) { process.destroyForcibly(); process.waitFor(); } }
                catch (InterruptedException error) { interrupted = true; process.destroyForcibly(); }
            }
        } finally {
            connection.close();
            if (lease != null && process != null && !process.isAlive()) try { Files.deleteIfExists(lease); } catch (IOException error) { throw new SdkException("lease", "Process stopped but lease cleanup failed", error); }
            if (interrupted) Thread.currentThread().interrupt();
        }
    }
}
