using PuppeteerSharp;
using Mimic.Sdk;
using Mimic.Sdk.Generated;
using System.Text.Json.Nodes;

namespace Mimic.PuppeteerSharp;

public sealed class PuppeteerSession : IAsyncDisposable
{
    private readonly RuntimeProcess runtime;
    private readonly HashSet<IBrowserContext> ownedContexts = [];
    private readonly SemaphoreSlim acquisition = new(1, 1);
    private int disposed;
    public IBrowser Browser { get; }
    public MimicClient Mimic { get; }
    public RuntimeProcess Runtime => runtime;
    private PuppeteerSession(RuntimeProcess runtime, IBrowser browser) { this.runtime = runtime; Browser = browser; Mimic = new(runtime.Transport); }

    public static async Task<PuppeteerSession> LaunchAsync(RuntimeOptions? options = null, ConnectOptions? connectOptions = null, CancellationToken cancellationToken = default)
        => await AttachAsync(await new RuntimeManager().LaunchAsync(options, cancellationToken), connectOptions, cancellationToken);
    public static async Task<PuppeteerSession> ConnectAsync(string endpoint, ConnectOptions? connectOptions = null, CancellationToken cancellationToken = default)
        => await AttachAsync(await RuntimeProcess.ConnectAsync(endpoint, cancellationToken), connectOptions, cancellationToken);
    private static async Task<PuppeteerSession> AttachAsync(RuntimeProcess runtime, ConnectOptions? options, CancellationToken cancellationToken)
    {
        IBrowser? browser = null;
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            options ??= new() { DefaultViewport = null };
            if (options.BrowserWSEndpoint is not null || options.BrowserURL is not null) throw new ArgumentException("Use the SDK endpoint or runtime options; connection options must not override the endpoint");
            options.BrowserWSEndpoint = runtime.WebSocketEndpoint.ToString();
            browser = await Puppeteer.ConnectAsync(options);
            cancellationToken.ThrowIfCancellationRequested();
            return new(runtime, browser);
        }
        catch { browser?.Disconnect(); await runtime.DisposeAsync(); throw; }
    }

    public async Task<IBrowserContext> NewContextAsync(BrowserContextOptions? options = null, JsonObject? media = null, JsonObject? resourcePolicy = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
    {
        if (media is not null && mediaFactory is not null) throw new ArgumentException("Select a media configuration or a media factory");
        var context = await CreateOwnedContextAsync(options);
        try
        {
            var capabilities = ForContext(context);
            if (mediaFactory is not null) media = Wire.Encode(await mediaFactory(new(capabilities.Id, Mimic), cancellationToken));
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            if (media is not null) await capabilities.SetMediaProfileAsync(media, cancellationToken);
            if (resourcePolicy is not null) await capabilities.SetResourcePolicyAsync(resourcePolicy, cancellationToken);
            return context;
        }
        catch { if (disposed == 0) { lock (ownedContexts) ownedContexts.Remove(context); await context.CloseAsync(); } throw; }
    }
    public MimicContext ForContext(IBrowserContext context)
    {
        if (!ReferenceEquals(context.Browser, Browser)) throw new ArgumentException("Context belongs to another browser");
        return Mimic.Context(context.Id ?? throw new ArgumentException("Default context has no explicit Context ID; use NewContextAsync"));
    }

    public Task<IBrowserContext> NewConfiguredContextAsync(ContextConfiguration configuration, BrowserContextOptions? options = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
        => NewConfiguredContextAsync(configuration.ToWire(), options, cancellationToken, mediaFactory);

    public async Task<IBrowserContext> NewConfiguredContextAsync(JsonObject configuration, BrowserContextOptions? options = null, CancellationToken cancellationToken = default, Func<ContextSetup, CancellationToken, Task<MediaConfiguration>>? mediaFactory = null)
    {
        configuration = configuration.DeepClone().AsObject();
        if (configuration.ContainsKey("media") && mediaFactory is not null) throw new ArgumentException("Select a media configuration or a media factory");
        var context = await CreateOwnedContextAsync(options);
        try
        {
            var capabilities = ForContext(context);
            if (mediaFactory is not null) configuration["media"] = Wire.Encode(await mediaFactory(new(capabilities.Id, Mimic), cancellationToken));
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            await capabilities.ConfigureAsync(configuration, cancellationToken);
            return context;
        }
        catch { if (disposed == 0) { lock (ownedContexts) ownedContexts.Remove(context); await context.CloseAsync(); } throw; }
    }

    private async Task<IBrowserContext> CreateOwnedContextAsync(BrowserContextOptions? options)
    {
        await acquisition.WaitAsync();
        try
        {
            ObjectDisposedException.ThrowIf(disposed != 0, this);
            var context = await Browser.CreateBrowserContextAsync(options);
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
        finally { Browser.Disconnect(); await runtime.DisposeAsync(); }
    }
}
