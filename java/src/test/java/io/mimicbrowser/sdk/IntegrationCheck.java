package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.microsoft.playwright.options.AriaRole;
import io.mimicbrowser.sdk.playwright.PlaywrightSession;
import java.nio.file.Files;
import java.nio.file.Path;

/** Explicit Linux-only integration entry point; never part of Windows unit tests. */
public final class IntegrationCheck {
    private static void check(boolean condition, String message) { if (!condition) throw new AssertionError(message); }
    public static void main(String[] args) throws Exception {
        check(System.getProperty("os.name").equals("Linux"), "Live integration qualification runs on Linux only");
        if (args.length == 3 && args[0].equals("--install-offline")) {
            var options = new RuntimeOptions().runtimeDirectory(Path.of(args[1])).archivePath(Path.of(args[2])).allowDownload(false);
            var installed = new RuntimeManager().install(options);
            RuntimeManager.verify(installed.directory(), installed.runtimeLock(), "linux-amd64");
            System.out.println("PASS Java verified offline archive/shared installation");
            return;
        }
        if (args.length == 2 && args[0].equals("--install")) {
            var options = new RuntimeOptions().runtimeDirectory(Path.of(args[1]));
            var installation = new RuntimeManager().install(options);
            var cached = new RuntimeManager().install(new RuntimeOptions().runtimeDirectory(Path.of(args[1])).allowDownload(false));
            check(installation.executablePath().equals(cached.executablePath()), "Offline install did not reuse cache");
            try (var runtime = new RuntimeManager().launch(new RuntimeOptions().runtimeDirectory(Path.of(args[1])).allowDownload(false))) {
                check(runtime.identity().get("version").getAsString().equals(installation.release()), "Installed runtime identity");
                try { RuntimeManager.prune(installation.directory(), options); throw new AssertionError("Pruned leased runtime"); }
                catch (SdkException error) { check(error.kind().equals("lease"), "Lease error kind"); }
            }
            System.out.println("PASS Java verified install/shared .NET cache, offline launch, active lease guard");
            return;
        }
        var candidate = args.length == 2 && args[0].equals("--candidate");
        check(args.length == 1 || candidate, "Usage: IntegrationCheck <runtime executable>, --candidate <executable>, or --install <cache>");
        var options = new RuntimeOptions().executablePath(Path.of(args[candidate ? 1 : 0])).allowDownload(false);
        Long pid;
        try (var session = PlaywrightSession.launch(options)) {
            pid = session.runtime().processId();
            var media = JsonParser.parseString("{\"devices\":[]}").getAsJsonObject();
            var policy = JsonParser.parseString("{\"reportOnly\":true}").getAsJsonObject();
            var context = session.newContext(null, media, policy);
            check(context.pages().isEmpty(), "Temporary Context bridge page leaked");
            var capabilities = session.forContext(context);
            check(capabilities.getMediaProfile().getAsJsonObject("profile").getAsJsonArray("devices").isEmpty(), "Media profile not configured");
            check(capabilities.getResourcePolicy().getAsJsonObject("policy").get("reportOnly").getAsBoolean(), "Resource policy not configured");
            var page = context.newPage();
            page.setContent("<title>Java native</title><button onclick=\"this.textContent='done'\">run</button>");
            page.getByRole(AriaRole.BUTTON).click();
            check(page.getByRole(AriaRole.BUTTON).textContent().equals("done"), "Native Playwright migration scenario");
            check(session.mimic().commands().getVersion(new Generated.GetVersionParams()).version.equals(session.runtime().identity().get("version").getAsString()), "Typed version response");
            try { session.mimic().experimental().send("Mimic.futureUnsupported", new JsonObject()); throw new AssertionError("Unknown method accepted"); }
            catch (ProtocolException error) { check(error.code() < 0 && !error.getMessage().isEmpty(), "Unknown method error code lost"); }
            var invalid = new Generated.GenerateProfileParams(); invalid.seed = Generated.OptionalValue.of(null);
            try { session.mimic().commands().generateProfile(invalid); throw new AssertionError("Invalid null accepted"); }
            catch (ProtocolException error) { check(error.code() == -32602 && error.data() != null, "Structured protocol error lost"); }
            try (var attached = PlaywrightSession.connect(session.runtime().endpoint().toString())) { attached.newContext().newPage(); }
            check(page.title().equals("Java native"), "Attached disposal affected existing client");
            if (candidate) {
                var config = JsonParser.parseString("{\"profile\":{\"generate\":{\"platform\":\"windows\",\"seed\":\"java-sdk-profile\"}},\"media\":{\"devices\":[]}}").getAsJsonObject();
                var configured = session.newConfiguredContext(config, null);
                check(configured.pages().isEmpty(), "Configured Context leaked bridge probe");
                check(configured.newPage().evaluate("navigator.platform").equals("Win32"), "Generated profile did not reach native page");
                config.remove("media");
                var factoryCalled = new java.util.concurrent.atomic.AtomicBoolean();
                var withFactory = session.newConfiguredContext(config, null, setup -> {
                    var query = new Generated.GetMediaSourcesParams(); query.browserContextId = Generated.OptionalValue.of(setup.browserContextId());
                    check(setup.mimic().commands().getMediaSources(query).sources != null, "Typed Context source discovery failed");
                    factoryCalled.set(true);
                    var mediaConfig = new Generated.MediaConfiguration(); mediaConfig.devices = Generated.OptionalValue.of(java.util.List.of()); return mediaConfig;
                });
                check(factoryCalled.get() && withFactory.pages().isEmpty(), "Media factory leaked a probe or was not called");
                check(withFactory.newPage().evaluate("navigator.platform").equals("Win32"), "Media factory broke managed profile");
                var contextCount = session.browser().contexts().size();
                try { session.newConfiguredContext(config, null, setup -> { throw new IllegalStateException("factory failure"); }); throw new AssertionError("Factory error swallowed"); }
                catch (IllegalStateException expected) { check(expected.getMessage().equals("factory failure"), "Factory exception changed"); }
                check(session.browser().contexts().size() == contextCount, "Failed media factory leaked Context");
                var beforeClosingFactory = session.mimic().send("Target.getBrowserContexts", new JsonObject());
                try (var callbackSession = PlaywrightSession.connect(session.runtime().endpoint().toString())) {
                    try {
                        callbackSession.newContext(null, null, null, setup -> {
                            callbackSession.close(); var empty = new Generated.MediaConfiguration(); empty.devices = Generated.OptionalValue.of(java.util.List.of()); return empty;
                        });
                        throw new AssertionError("Closed callback session returned Context");
                    } catch (IllegalStateException expected) { check(expected.getMessage().contains("closed"), "Callback shutdown error changed"); }
                }
                check(beforeClosingFactory.equals(session.mimic().send("Target.getBrowserContexts", new JsonObject())), "Closing factory leaked Context");
            }
        }
        check(!Files.exists(Path.of("/proc/" + pid)), "Owned runtime survived disposal");
        var temporary = Files.createTempDirectory("mimic-java-timeout-");
        try {
            var fake = temporary.resolve("runtime");
            Files.writeString(fake, "#!/bin/sh\necho $$ > \"" + temporary.resolve("pid") + "\"\nexec sleep 60\n");
            Files.setPosixFilePermissions(fake, java.nio.file.attribute.PosixFilePermissions.fromString("rwx------"));
            try { new RuntimeManager().launch(new RuntimeOptions().executablePath(fake).allowDownload(false).timeout(java.time.Duration.ofMillis(200))); throw new AssertionError("Timeout ignored"); }
            catch (SdkException error) { check(error.kind().equals("startup"), "Startup timeout kind"); }
            check(!Files.exists(Path.of("/proc/" + Files.readString(temporary.resolve("pid")).trim())), "Timed out child survived");
            var failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            var interrupted = new java.util.concurrent.atomic.AtomicBoolean();
            Files.delete(temporary.resolve("pid"));
            var worker = new Thread(() -> {
                try { new RuntimeManager().launch(new RuntimeOptions().executablePath(fake).allowDownload(false)); failure.set(new AssertionError("Interruption ignored")); }
                catch (SdkException error) { if (!error.kind().equals("cancelled")) failure.set(error); interrupted.set(Thread.currentThread().isInterrupted()); }
            });
            worker.start();
            var deadline = System.nanoTime() + java.time.Duration.ofSeconds(5).toNanos();
            while (!Files.exists(temporary.resolve("pid")) && System.nanoTime() < deadline) Thread.sleep(10);
            worker.interrupt(); worker.join(5000);
            check(!worker.isAlive() && failure.get() == null && interrupted.get(), "Cancelled startup lost interruption/cleanup: " + failure.get());
            check(!Files.exists(Path.of("/proc/" + Files.readString(temporary.resolve("pid")).trim())), "Interrupted child survived");
        } finally { try (var paths = Files.walk(temporary)) { for (var path : paths.sorted(java.util.Comparator.reverseOrder()).toList()) Files.delete(path); } }
        System.out.println("PASS Java real Playwright, Context bridge, typed/raw errors, attach isolation, owned teardown");
    }
}
