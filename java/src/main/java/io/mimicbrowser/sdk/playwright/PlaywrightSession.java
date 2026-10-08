package io.mimicbrowser.sdk.playwright;

import com.google.gson.JsonObject;
import com.microsoft.playwright.Browser;
import com.microsoft.playwright.BrowserContext;
import com.microsoft.playwright.BrowserType;
import com.microsoft.playwright.CDPSession;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.Playwright;
import io.mimicbrowser.sdk.MimicClient;
import io.mimicbrowser.sdk.ContextSetup;
import io.mimicbrowser.sdk.Generated;
import io.mimicbrowser.sdk.MimicContext;
import io.mimicbrowser.sdk.RuntimeManager;
import io.mimicbrowser.sdk.RuntimeOptions;
import io.mimicbrowser.sdk.RuntimeProcess;
import java.util.ArrayList;
import java.util.HashMap;

/** Optional Playwright Java adapter; all Browser/Context/Page values are native. */
public final class PlaywrightSession implements AutoCloseable {
    private final RuntimeProcess runtime;
    private final Playwright driver;
    private final boolean ownsDriver;
    private final Browser browser;
    private final MimicClient mimic;
    private final ArrayList<BrowserContext> ownedContexts = new ArrayList<>();
    private boolean closed;
    private PlaywrightSession(RuntimeProcess runtime, Playwright driver, boolean ownsDriver, Browser browser) {
        this.runtime = runtime; this.driver = driver; this.ownsDriver = ownsDriver; this.browser = browser; mimic = new MimicClient(runtime.transport());
    }
    public Browser browser() { return browser; }
    public MimicClient mimic() { return mimic; }
    public RuntimeProcess runtime() { return runtime; }
    public static PlaywrightSession launch(RuntimeOptions options) { return launch(options, null, null); }
    public static PlaywrightSession launch(RuntimeOptions options, Playwright borrowed, BrowserType.ConnectOverCDPOptions connectOptions) {
        return attach(new RuntimeManager().launch(options), borrowed, connectOptions);
    }
    public static PlaywrightSession connect(String endpoint) { return connect(endpoint, null, null); }
    public static PlaywrightSession connect(String endpoint, Playwright borrowed, BrowserType.ConnectOverCDPOptions connectOptions) {
        return attach(RuntimeProcess.connect(endpoint), borrowed, connectOptions);
    }
    private static PlaywrightSession attach(RuntimeProcess runtime, Playwright borrowed, BrowserType.ConnectOverCDPOptions options) {
        Playwright driver = borrowed;
        Browser browser = null;
        try {
            if (driver == null) {
                var environment = new HashMap<>(System.getenv());
                environment.put("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1");
                driver = Playwright.create(new Playwright.CreateOptions().setEnv(environment));
            }
            browser = driver.chromium().connectOverCDP(runtime.webSocketEndpoint().toString(), options);
            return new PlaywrightSession(runtime, driver, borrowed == null, browser);
        } catch (RuntimeException error) {
            if (browser != null) browser.close();
            if (borrowed == null && driver != null) driver.close();
            runtime.close(); throw error;
        }
    }
    public BrowserContext newContext(Browser.NewContextOptions options, JsonObject media, JsonObject resourcePolicy) {
        return newContext(options, media, resourcePolicy, null);
    }
    public BrowserContext newContext(Browser.NewContextOptions options, JsonObject media, JsonObject resourcePolicy, java.util.function.Function<ContextSetup, Generated.MediaConfiguration> mediaFactory) {
        if (media != null && mediaFactory != null) throw new IllegalArgumentException("Select a media configuration or a media factory");
        var context = browser.newContext(options);
        try {
            if (closed) throw new IllegalStateException("Session is closed");
            ownedContexts.add(context);
            var capabilities = forContext(context);
            if (mediaFactory != null) media = Generated.toWire(mediaFactory.apply(new ContextSetup(capabilities.id(), mimic))).getAsJsonObject();
            if (closed) throw new IllegalStateException("Session closed during Context setup");
            if (media != null) capabilities.setMediaProfile(media);
            if (resourcePolicy != null) capabilities.setResourcePolicy(resourcePolicy);
            return context;
        } catch (RuntimeException error) { ownedContexts.remove(context); if (!closed) context.close(); throw error; }
    }
    public BrowserContext newContext() { return newContext(null, null, null); }
    public BrowserContext newConfiguredContext(JsonObject configuration, Browser.NewContextOptions options) {
        return newConfiguredContext(configuration, options, null);
    }
    public BrowserContext newConfiguredContext(JsonObject configuration, Browser.NewContextOptions options, java.util.function.Function<ContextSetup, Generated.MediaConfiguration> mediaFactory) {
        configuration = configuration.deepCopy();
        if (configuration.has("media") && mediaFactory != null) throw new IllegalArgumentException("Select a media configuration or a media factory");
        options = copyOptions(options);
        if (configuration.has("profile")) {
            if (options.viewportSize != null && options.viewportSize.isPresent() || options.screenSize != null || options.deviceScaleFactor != null || options.isMobile != null || options.hasTouch != null || options.userAgent != null || options.locale != null || options.timezoneId != null || options.colorScheme != null && options.colorScheme.isPresent() || options.reducedMotion != null && options.reducedMotion.isPresent() || options.forcedColors != null && options.forcedColors.isPresent() || options.contrast != null && options.contrast.isPresent())
                throw new IllegalArgumentException("A managed Mimic profile cannot be combined with Playwright identity, geometry or media emulation options");
            options.setViewportSize(null).setColorScheme(null).setReducedMotion(null).setForcedColors(null).setContrast(null);
        }
        var context = browser.newContext(options);
        try {
            if (closed) throw new IllegalStateException("Session is closed");
            ownedContexts.add(context);
            var capabilities = forContext(context);
            if (mediaFactory != null) configuration.add("media", Generated.toWire(mediaFactory.apply(new ContextSetup(capabilities.id(), mimic))));
            if (closed) throw new IllegalStateException("Session closed during Context setup");
            capabilities.configure(configuration); return context;
        }
        catch (RuntimeException error) { ownedContexts.remove(context); if (!closed) context.close(); throw error; }
    }
    private static Browser.NewContextOptions copyOptions(Browser.NewContextOptions source) {
        var target = new Browser.NewContextOptions();
        if (source == null) return target;
        // Playwright exposes its options as public fields; copy them without touching implementation internals.
        try { for (var field : Browser.NewContextOptions.class.getFields()) field.set(target, field.get(source)); }
        catch (IllegalAccessException impossible) { throw new AssertionError(impossible); }
        return target;
    }
    public MimicContext forContext(BrowserContext context) {
        if (context.browser() != browser) throw new IllegalArgumentException("Context belongs to another browser");
        Page probe = null;
        CDPSession session = null;
        try {
            var pages = context.pages();
            var page = pages.isEmpty() ? (probe = context.newPage()) : pages.get(0);
            session = context.newCDPSession(page);
            var target = session.send("Target.getTargetInfo").getAsJsonObject("targetInfo");
            return mimic.context(target.get("browserContextId").getAsString());
        } finally {
            try { if (session != null) session.detach(); }
            finally { if (probe != null) probe.close(); }
        }
    }
    @Override public void close() {
        if (closed) return;
        closed = true;
        try { for (var context : ownedContexts) context.close(); }
        finally {
            try { browser.close(); } // CDP-connected Playwright disconnects, preserving the remote server.
            finally { try { if (ownsDriver) driver.close(); } finally { runtime.close(); } }
        }
    }
}
