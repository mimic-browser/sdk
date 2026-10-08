package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import static org.junit.jupiter.api.Assertions.*;

class SdkTest {
    @TempDir Path temporary;
    @Test void sharedWireCorpus() throws Exception {
        var corpus = JsonParser.parseString(Files.readString(Path.of("../conformance/fixtures/wire.json"))).getAsJsonObject();
        int count = 0;
        for (var entry : corpus.getAsJsonArray("cases")) {
            var item = entry.getAsJsonObject();
            if (!item.get("valid").getAsBoolean() && !item.get("name").getAsString().equals("explicit_null_is_not_omission")) continue;
            var type = Class.forName("io.mimicbrowser.sdk.Generated$" + item.get("model").getAsString());
            var model = Generated.fromWire(type, item.get("wire"));
            assertEquals(item.get("wire"), Generated.toWire(model), item.get("name").getAsString());
            count++;
        }
        assertTrue(count >= 21);
    }
    @Test void exactSelectorsAndProvenance() {
        assertEquals("v0.2.2", RuntimeManager.normalizeVersion("0.2.2"));
        for (var invalid : new String[] { "latest", "^0.2.2", "v01.2.2", "0.2", "../../escape", "0.2.2+meta" })
            assertThrows(SdkException.class, () -> RuntimeManager.normalizeVersion(invalid));
        var lock = RuntimeManager.defaultLock();
        RuntimeManager.validateLock(lock, null);
        var corrupt = lock.deepCopy(); corrupt.getAsJsonObject("manifest").addProperty("version", "v0.2.3");
        assertThrows(SdkException.class, () -> RuntimeManager.validateLock(corrupt, null));
        var mismatch = lock.deepCopy(); mismatch.addProperty("manifestJson", "{}");
        assertThrows(SdkException.class, () -> RuntimeManager.validateLock(mismatch, null));
        assertThrows(SdkException.class, () -> RuntimeManager.validateLock(lock, "v0.2.3"));
    }
    @Test void archiveTraversalRejected() throws Exception {
        var archive = temporary.resolve("unsafe.zip");
        try (var output = new ZipOutputStream(Files.newOutputStream(archive))) {
            output.putNextEntry(new ZipEntry("../escape")); output.write(42); output.closeEntry();
        }
        assertThrows(SdkException.class, () -> RuntimeManager.extractArchive(archive, temporary.resolve("tree"), "root", true));
        assertFalse(Files.exists(temporary.resolve("escape")));
    }
    @Test void archiveCopiesOnlyDeclaredTree() throws Exception {
        var archive = temporary.resolve("safe.zip");
        try (var output = new ZipOutputStream(Files.newOutputStream(archive))) {
            output.putNextEntry(new ZipEntry("root/mimic")); output.write(new byte[] { 1, 2, 3 }); output.closeEntry();
            output.putNextEntry(new ZipEntry("root/LICENSE")); output.write("license".getBytes()); output.closeEntry();
        }
        var tree = temporary.resolve("tree"); Files.createDirectory(tree);
        RuntimeManager.extractArchive(archive, tree, "root", true);
        assertArrayEquals(new byte[] { 1, 2, 3 }, Files.readAllBytes(tree.resolve("mimic")));
        assertEquals("license", Files.readString(tree.resolve("LICENSE")));
    }
    @Test void offlineMissingAndVersionLockConflict() throws Exception {
        var lockPath = temporary.resolve("runtime-lock.json"); Files.writeString(lockPath, RuntimeManager.defaultLock().toString());
        var conflict = new RuntimeOptions().version("0.2.3").lockFile(lockPath).allowDownload(false);
        assertEquals("configuration", assertThrows(SdkException.class, () -> new RuntimeManager().resolveLock(conflict)).kind());
        var missing = new RuntimeOptions().runtimeDirectory(temporary).allowDownload(false);
        assertEquals("offline", assertThrows(SdkException.class, () -> new RuntimeManager().install(missing)).kind());
    }
    @Test void explicitExecutableDoesNotResolveOtherReleaseOnline() throws Exception {
        var binary = temporary.resolve(RuntimeManager.windows() ? "mimic.exe" : "mimic"); Files.writeString(binary, "fixture");
        var result = new RuntimeManager().install(new RuntimeOptions().executablePath(binary).version("0.9.9").allowDownload(false));
        assertEquals("v0.9.9", result.release());
        assertEquals(binary, result.executablePath());
    }
    @Test void explicitLockedBinaryMustMatchHash() throws Exception {
        var binary = temporary.resolve("mimic"); Files.writeString(binary, "fixture");
        var lockPath = temporary.resolve("runtime-lock.json"); Files.writeString(lockPath, RuntimeManager.defaultLock().toString());
        var options = new RuntimeOptions().executablePath(binary).lockFile(lockPath).allowDownload(false);
        assertEquals("integrity", assertThrows(SdkException.class, () -> new RuntimeManager().install(options)).kind());
    }
    @Test void typedAndExperimentalShareTransportAndContextScope() {
        var calls = new java.util.ArrayList<JsonObject>();
        ProtocolTransport capture = (method, params) -> { var request = new JsonObject(); request.addProperty("method", method); request.add("params", params.deepCopy()); calls.add(request); return new JsonObject(); };
        var client = new MimicClient(capture);
        var raw = JsonParser.parseString("{\"unknown\":null,\"large\":9007199254740991,\"values\":[false,0,null]}").getAsJsonObject();
        client.experimental().send("Mimic.futureCommand", raw);
        assertEquals(raw, calls.get(0).get("params"));
        client.context("context-x").setMediaProfile(JsonParser.parseString("{\"devices\":[]}").getAsJsonObject());
        assertEquals("Mimic.setMediaProfile", calls.get(1).get("method").getAsString());
        assertEquals("context-x", calls.get(1).getAsJsonObject("params").get("browserContextId").getAsString());
        assertTrue(calls.get(1).getAsJsonObject("params").has("devices"));
        client.context("context-x").setResourcePolicy(new JsonObject());
        assertEquals("Mimic.updateResourcePolicy", calls.get(2).get("method").getAsString());
        assertTrue(calls.get(2).getAsJsonObject("params").has("policy"));
    }
}
