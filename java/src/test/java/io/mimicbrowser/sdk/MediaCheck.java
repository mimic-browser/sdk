package io.mimicbrowser.sdk;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import io.mimicbrowser.sdk.playwright.PlaywrightSession;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/** Runs only against the explicit synthetic provider fixture, never native hardware. */
public final class MediaCheck {
    private static void check(boolean value, String message) { if (!value) throw new AssertionError(message); }
    private static JsonObject state(HttpClient http, String fixture) throws Exception {
        return JsonParser.parseString(http.send(HttpRequest.newBuilder(URI.create(fixture + "/state")).GET().build(), HttpResponse.BodyHandlers.ofString()).body()).getAsJsonObject();
    }
    public static void main(String[] args) throws Exception {
        check(System.getProperty("os.name").equals("Linux"), "Synthetic media tests are Linux-only");
        var endpoint = args[0]; var fixture = args[1]; var http = HttpClient.newHttpClient(); var before = state(http, fixture);
        var identities = new String[2];
        JsonObject observed;
        try (var session = PlaywrightSession.connect(endpoint)) {
            var config = JsonParser.parseString("{\"profile\":{\"generate\":{\"seed\":\"java-media-environment\"}}}").getAsJsonObject();
            var context = session.newConfiguredContext(config, null, setup -> {
                identities[0] = setup.browserContextId();
                var query = new Generated.GetMediaSourcesParams(); query.browserContextId = Generated.OptionalValue.of(setup.browserContextId());
                var sources = setup.mimic().commands().getMediaSources(query).sources;
                var camera = sources.stream().filter(source -> source.label.equals("Private native camera B")).findFirst().orElseThrow();
                var microphone = sources.stream().filter(source -> source.label.equals("Private native microphone B")).findFirst().orElseThrow();
                identities[1] = camera.sourceId;
                var media = JsonParser.parseString("{\"devices\":[{\"key\":\"front\",\"kind\":\"videoinput\",\"source\":{},\"label\":\"Studio Camera\",\"group\":\"desk\",\"modes\":[{\"width\":16,\"height\":8,\"frameRate\":30}],\"defaultMode\":{\"width\":16,\"height\":8,\"frameRate\":30},\"processing\":{\"resize\":\"crop-and-scale\"}},{\"key\":\"voice\",\"kind\":\"audioinput\",\"source\":{},\"label\":\"Studio Microphone\",\"group\":\"desk\"}]}").getAsJsonObject();
                media.getAsJsonArray("devices").get(0).getAsJsonObject().getAsJsonObject("source").addProperty("sourceId", camera.sourceId);
                media.getAsJsonArray("devices").get(1).getAsJsonObject().getAsJsonObject("source").addProperty("sourceId", microphone.sourceId);
                return Generated.fromWire(Generated.MediaConfiguration.class, media);
            });
            check(context.pages().isEmpty(), "Media setup leaked probe page");
            check(state(http, fixture).equals(before), "Discovery/configuration opened capture");
            var query = new Generated.GetMediaSourcesParams(); query.browserContextId = Generated.OptionalValue.of(identities[0]);
            check(session.mimic().commands().getMediaSources(query).sources.stream().anyMatch(source -> source.sourceId.equals(identities[1])), "Profile configuration invalidated Context source ID");
            var permissions = JsonParser.parseString("{\"permissions\":[\"videoCapture\",\"audioCapture\"]}").getAsJsonObject();
            permissions.addProperty("browserContextId", identities[0]); permissions.addProperty("origin", fixture);
            session.mimic().send("Browser.grantPermissions", permissions);
            var page = context.newPage(); page.navigate(fixture); page.mouse().click(1, 1);
            var script = Files.readString(Path.of("../dotnet/Mimic.Tests/media_capture.js")).trim().replaceAll(";$", "");
            observed = new Gson().toJsonTree(page.evaluate(script)).getAsJsonObject();
        }
        var pixel = observed.getAsJsonArray("pixel");
        check(pixel.size() == 4 && pixel.get(0).getAsInt() == 0 && pixel.get(1).getAsInt() == 0 && pixel.get(2).getAsInt() == 255 && pixel.get(3).getAsInt() == 255, "Private B camera did not deliver blue pixels");
        var audio = observed.getAsJsonObject("audioEnergy");
        check(audio.get("b").getAsDouble() > 1 && audio.get("b").getAsDouble() > 5 * audio.get("a").getAsDouble(), "Private B microphone PCM did not reach Web Audio");
        check(!observed.toString().contains("Private native") && !observed.toString().contains(identities[1]), "Private identity leaked to page");
        var devices = observed.getAsJsonArray("devices");
        check(devices.size() == 2 && devices.get(0).getAsJsonObject().get("groupId").equals(devices.get(1).getAsJsonObject().get("groupId")), "Public media grouping changed");
        JsonObject last = null;
        for (var attempt = 0; attempt < 100; attempt++) {
            last = state(http, fixture); var current = last;
            if (last.getAsJsonObject("opens").entrySet().stream().allMatch(entry -> entry.getValue().equals(current.getAsJsonObject("closes").get(entry.getKey())))) break;
            Thread.sleep(20);
        }
        for (var source : List.of("native-camera-b", "native-microphone-b")) {
            var previous = before.getAsJsonObject("opens").has(source) ? before.getAsJsonObject("opens").get(source).getAsInt() : 0;
            check(last.getAsJsonObject("opens").get(source).getAsInt() == previous + 1 && last.getAsJsonObject("closes").get(source).getAsInt() == previous + 1, "Private capture worker leaked or wrong source opened");
        }
        check(java.util.Objects.equals(last.getAsJsonObject("opens").get("native-camera-a"), before.getAsJsonObject("opens").get("native-camera-a")), "Capture silently selected source A");
        System.out.println("PASS Java Playwright public identities, private B blue frames/660 Hz PCM, capture cleanup");
    }
}
