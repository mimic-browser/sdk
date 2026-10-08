using Microsoft.Playwright;
using Mimic.Sdk;
using Mimic.Sdk.Generated;
using System.Text.Json.Nodes;

namespace Mimic.Playwright;

public sealed class PlaywrightSession : IAsyncDisposable
{
    private readonly RuntimeProcess runtime;
    private readonly IPlaywright driver;
    private readonly bool ownsDriver;
    private readonly HashSet<IBrowserContext> ownedContexts = [];
    private readonly SemaphoreSlim acquisition = new(1, 1);
    private int disposed;
    public IBrowser Browser { get; }
    public MimicClient Mimic { get; }
    public RuntimeProcess Runtime => runtime;
    private PlaywrightSession(RuntimeProcess runtime, IPlaywright driver, bool ownsDriver, IBrowser browser)
    { this.runtime = runtime; this.driver = driver; this.ownsDriver = ownsDriver; Browser = browser; Mimic = new(runtime.Transport); }

    public static async Task<PlaywrightSession> LaunchAsync(RuntimeOptions? options = null, IPlaywright? playwright = null, BrowserTypeConnectOverCDPOptions? connectOptions = null, CancellationToken cancellationToken = default)
    {
        var runtime = await new RuntimeManager().LaunchAsync(options, cancellationToken);
        return await AttachAsync(runtime, playwright, connectOptions, cancellationToken);
    }
    public static async Task<PlaywrightSession> ConnectAsync(string endpoint, IPlaywright? playwright = null, BrowserTypeConnectOverCDPOptions? connectOptions = null, CancellationToken cancellationToken = default)
    {
        var runtime = await RuntimeProcess.ConnectAsync(endpoint, cancellationToken);
        return await AttachAsync(runtime, playwright, connectOptions, cancellationToken);
    }
    private static async Task<PlaywrightSession> AttachAsync(RuntimeProcess runtime, IPlaywright? playwright, BrowserTypeConnectOverCDPOptions? options, CancellationToken cancellationToken)
    {
        IPlaywright? driver = playwright;
        IBrowser? browser = null;
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            driver ??= await Microsoft.Playwright.Playwright.CreateAsync();
            browser = await driver.Chromium.ConnectOverCDPAsync(runtime.WebSocketEndpoint.ToString(), options);
            cancellationToken.ThrowIfCancellationRequested();
            return new(runtime, driver, playwright is null, browser);
        }
        catch
        {
            if (browser is not null) await browser.CloseAsync();
            if (playwright is null) driver?.Dispose();
            await runtime.DisposeAsync();
            throw;
        }
    }

    /// <summary>Creates a native context and configures Mimic before returning it.</summary>
    public async Task<IBrowserContext> NewContextAsync(BrowserNewContextOptions? options = null, JsonObject? media = null, JsonObject? resourcePolicy = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
    {
        if (media is not null && mediaFactory is not null) throw new ArgumentException("Select a media configuration or a media factory");
        var context = await CreateOwnedContextAsync(options);
        try
        {
            var capabilities = await ForContextAsync(context, cancellationToken);
            if (mediaFactory is not null) media = Wire.Encode(await mediaFactory(new(capabilities.Id, Mimic), cancellationToken));
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            if (media is not null) await capabilities.SetMediaProfileAsync(media, cancellationToken);
            if (resourcePolicy is not null) await capabilities.SetResourcePolicyAsync(resourcePolicy, cancellationToken);
            return context;
        }
        catch { if (disposed == 0) { lock (ownedContexts) ownedContexts.Remove(context); await context.CloseAsync(); } throw; }
    }

    public async Task<MimicContext> ForContextAsync(IBrowserContext context, CancellationToken cancellationToken = default)
    {
        if (!ReferenceEquals(context.Browser, Browser)) throw new ArgumentException("Context belongs to another browser");
        IPage? probe = null;
        ICDPSession? session = null;
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var page = context.Pages.FirstOrDefault() ?? (probe = await context.NewPageAsync());
            session = await context.NewCDPSessionAsync(page);
            var result = await session.SendAsync("Target.getTargetInfo");
            var id = result!.Value.GetProperty("targetInfo").GetProperty("browserContextId").GetString()!;
            cancellationToken.ThrowIfCancellationRequested();
            return Mimic.Context(id);
        }
        finally
        {
            if (session is not null) await session.DetachAsync();
            if (probe is not null) await probe.CloseAsync();
        }
    }

    public Task<IBrowserContext> NewConfiguredContextAsync(ContextConfiguration configuration, BrowserNewContextOptions? options = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
        => NewConfiguredContextAsync(configuration.ToWire(), options, cancellationToken, mediaFactory);

    public async Task<IBrowserContext> NewConfiguredContextAsync(JsonObject configuration, BrowserNewContextOptions? options = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
    {
        configuration = configuration.DeepClone().AsObject();
        if (configuration.ContainsKey("media") && mediaFactory is not null) throw new ArgumentException("Select a media configuration or a media factory");
        options = options is null ? new() : new(options);
        if (configuration.ContainsKey("profile"))
        {
            if (options.ViewportSize is not null && options.ViewportSize != ViewportSize.NoViewport || options.ScreenSize is not null || options.DeviceScaleFactor is not null || options.IsMobile is not null || options.HasTouch is not null || options.UserAgent is not null || options.Locale is not null || options.TimezoneId is not null || options.ColorScheme is not null && options.ColorScheme != ColorScheme.Null || options.ReducedMotion is not null && options.ReducedMotion != ReducedMotion.Null || options.ForcedColors is not null && options.ForcedColors != ForcedColors.Null || options.Contrast is not null && options.Contrast != Contrast.Null)
                throw new ArgumentException("A managed Mimic profile cannot be combined with Playwright identity, geometry or media emulation options", nameof(options));
            options.ViewportSize = ViewportSize.NoViewport;
            options.ColorScheme = ColorScheme.Null;
            options.ReducedMotion = ReducedMotion.Null;
            options.ForcedColors = ForcedColors.Null;
            options.Contrast = Contrast.Null;
        }
        var context = await CreateOwnedContextAsync(options);
        try
        {
            var capabilities = await ForContextAsync(context, cancellationToken);
            if (mediaFactory is not null) configuration["media"] = Wire.Encode(await mediaFactory(new(capabilities.Id, Mimic), cancellationToken));
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            await capabilities.ConfigureAsync(configuration, cancellationToken);
            return context;
        }
        catch { if (disposed == 0) { lock (ownedContexts) ownedContexts.Remove(context); await context.CloseAsync(); } throw; }
    }

    private async Task<IBrowserContext> CreateOwnedContextAsync(BrowserNewContextOptions? options)
    {
        await acquisition.WaitAsync();
        try
        {
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            var context = await Browser.NewContextAsync(options);
            lock (ownedContexts) ownedContexts.Add(context);
            return context;
        }
        finally { acquisition.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0) return;
        await acquisition.WaitAsync(); acquisition.Release();
        try
        {
            IBrowserContext[] contexts; lock (ownedContexts) contexts = [.. ownedContexts];
            foreach (var context in contexts) await context.CloseAsync();
        }
        finally
        {
            try { await Browser.CloseAsync(); } // CDP-attached Playwright closes its connection, not the remote browser.
            finally { if (ownsDriver) driver.Dispose(); await runtime.DisposeAsync(); }
        }
    }
}
