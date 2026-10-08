package io.mimicbrowser.sdk;

import com.google.gson.JsonParser;
import java.net.URI;
import java.time.Duration;

/** Driven by the shared Linux-only adversarial WebSocket fixture. */
public final class ProtocolCheck {
    public static void main(String[] args) {
        if (!System.getProperty("os.name").equals("Linux")) throw new AssertionError("Linux-only transport test");
        try (var connection = ProtocolConnection.connect(URI.create(args[0]))) {
            var input = JsonParser.parseString("{\"future\":null,\"values\":[false,9007199254740991]}").getAsJsonObject();
            var result = connection.session("owned-session").send("Fixture.session", input);
            if (!result.get("owner").getAsString().equals("owned-session") || !result.get("echo").equals(input)) throw new AssertionError("Foreign session reply routed to request");
            try { connection.send("Fixture.error", null); throw new AssertionError("Expected protocol error"); }
            catch (ProtocolException error) {
                if (error.code() != -32123 || !error.getMessage().equals("precise native error") || !error.data().isJsonArray() || error.data().getAsJsonArray().size() != 3 || !error.data().getAsJsonArray().get(0).isJsonNull()) throw new AssertionError("Raw arbitrary error data lost");
            }
            connection.timeout(Duration.ofMillis(100));
            try { connection.send("Fixture.timeout", null); throw new AssertionError("Expected timeout"); }
            catch (SdkException error) { if (!error.kind().equals("timeout")) throw error; }
        }
        System.out.println("PASS Java raw foreign-session rejection, exact JSON/error data and timeout");
    }
}
