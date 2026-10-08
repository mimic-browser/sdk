using System.IO.Compression;
using System.Text.Json;
using System.Text.Json.Nodes;
using Mimic.Sdk;
using Mimic.Sdk.Generated;
using Mimic.Playwright;
using Mimic.PuppeteerSharp;

static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
static void Fails(Action action, string message)
{
    try { action(); } catch (Mimic.Sdk.RuntimeException) { return; }
    throw new Exception(message);
}

var fixture = JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "wire.json")))!;
var cases = 0;
foreach (var item in fixture["cases"]!.AsArray())
{
    if (!item!["valid"]!.GetValue<bool>() && item["name"]!.GetValue<string>() != "explicit_null_is_not_omission") continue;
    var type = typeof(MimicCommands).Assembly.GetType("Mimic.Sdk.Generated." + item["model"]!.GetValue<string>())!;
    var wire = item["wire"]!.AsObject();
    var model = JsonSerializer.Deserialize(wire.ToJsonString(), type, Wire.Options);
    var output = JsonSerializer.SerializeToNode(model, type, Wire.Options);
    Check(JsonNode.DeepEquals(wire, output), "Wire round trip failed: " + item["name"]);
    cases++;
}
Check(RuntimeManager.NormalizeVersion("0.2.2") == "v0.2.2", "Exact pin normalization");
foreach (var invalid in new[] { "latest", "^0.2.2", "v01.2.2", "0.2", "../../bad", "0.2.2+meta" }) Fails(() => RuntimeManager.NormalizeVersion(invalid), "Accepted unsafe version " + invalid);
var runtimeLock = RuntimeManager.DefaultLock();
RuntimeManager.ValidateLock(runtimeLock);
var corrupt = runtimeLock.DeepClone().AsObject(); corrupt["manifest"]!["version"] = "v0.2.3";
Fails(() => RuntimeManager.ValidateLock(corrupt), "Accepted mismatched manifest");
corrupt = runtimeLock.DeepClone().AsObject(); corrupt["manifestJson"] = "{}";
Fails(() => RuntimeManager.ValidateLock(corrupt), "Accepted corrupt manifest bytes");
var temporary = Path.Combine(Path.GetTempPath(), "mimic-dotnet-test-" + Guid.NewGuid()); Directory.CreateDirectory(temporary);
try
{
    var archive = Path.Combine(temporary, "unsafe.zip");
    using (var zip = ZipFile.Open(archive, ZipArchiveMode.Create)) { using var writer = new StreamWriter(zip.CreateEntry("../escape").Open()); writer.Write("bad"); }
    Fails(() => RuntimeManager.ExtractArchive(archive, Path.Combine(temporary, "tree"), "root", true), "Accepted path traversal archive");
    Check(!File.Exists(Path.Combine(temporary, "escape")), "Archive escaped extraction");
    var lockPath = Path.Combine(temporary, "runtime-lock.json"); await File.WriteAllTextAsync(lockPath, runtimeLock.ToJsonString());
    try { await new RuntimeManager().ResolveLockAsync(new() { Version = "0.2.3", LockFile = lockPath, AllowDownload = false }); throw new Exception("Accepted conflicting explicit lock"); }
    catch (Mimic.Sdk.RuntimeException error) { Check(error.Kind == "configuration", "Conflict error kind"); }
    try { await new RuntimeManager().InstallAsync(new() { RuntimeDirectory = temporary, AllowDownload = false }); throw new Exception("Offline install unexpectedly worked"); }
    catch (Mimic.Sdk.RuntimeException error) { Check(error.Kind == "offline", "Offline error kind"); }
    var capture = new CaptureTransport();
    var client = new MimicClient(capture);
    var raw = JsonNode.Parse("{\"unknown\":null,\"large\":9007199254740991,\"values\":[false,0,null]}")!.AsObject();
    await client.Experimental.SendAsync("Mimic.futureCommand", raw);
    Check(capture.Method == "Mimic.futureCommand" && JsonNode.DeepEquals(capture.Parameters, raw), "Experimental dispatch altered JSON");
    await client.Context("context-x").SetMediaProfileAsync(new JsonObject { ["devices"] = new JsonArray() });
    Check(capture.Method == "Mimic.setMediaProfile" && capture.Parameters!["browserContextId"]!.GetValue<string>() == "context-x" && capture.Parameters["devices"] is JsonArray, "Context media scope");
    await client.Context("context-x").SetResourcePolicyAsync(new JsonObject());
    Check(capture.Method == "Mimic.updateResourcePolicy" && capture.Parameters!["policy"] is JsonObject, "Resource policy wire method");
}
finally { Directory.Delete(temporary, true); }
Console.WriteLine($"PASS .NET offline unit checks and {cases} shared wire round trips");

if (args.Length == 3 && args[0] == "--media")
{
    await MediaCheck.RunAsync(args[1], args[2]);
}
else if (args.Length == 2 && args[0] == "--transport")
{
    Check(OperatingSystem.IsLinux(), "Transport fixture must run inside Linux");
    await using var connection = await ProtocolConnection.ConnectAsync(new Uri(args[1]));
    var input = JsonNode.Parse("{\"future\":null,\"values\":[false,9007199254740991]}")!.AsObject();
    var result = await connection.Session("owned-session").SendAsync("Fixture.session", input);
    Check(result["owner"]!.GetValue<string>() == "owned-session" && JsonNode.DeepEquals(result["echo"], input), "Foreign session reply routed to request");
    try { await connection.SendAsync("Fixture.error"); throw new Exception("Expected protocol error"); }
    catch (ProtocolException error) { Check(error.Code == -32123 && error.Message == "precise native error" && error.DataValue is JsonArray data && data.Count == 3 && data[0] is null, "Raw arbitrary error data was lost"); }
    connection.CommandTimeout = TimeSpan.FromMilliseconds(100);
    try { await connection.SendAsync("Fixture.timeout"); throw new Exception("Expected timeout"); }
    catch (OperationCanceledException) { }
    Console.WriteLine("PASS .NET raw foreign-session rejection, exact JSON/error data and timeout");
}
else if (args.Length == 2 && args[0] is "--integration" or "--integration-candidate")
{
    Check(OperatingSystem.IsLinux(), "Live integration tests are Linux-only; Windows must not start listeners");
    var options = new RuntimeOptions { ExecutablePath = args[1], AllowDownload = false };
    await using var playwright = await PlaywrightSession.LaunchAsync(options);
    var context = await playwright.NewContextAsync(media: new JsonObject { ["devices"] = new JsonArray() }, resourcePolicy: new JsonObject { ["reportOnly"] = true });
    Check(context.Pages.Count == 0, "Temporary Context bridge probe leaked");
    var capabilities = await playwright.ForContextAsync(context);
    Check((await capabilities.GetMediaProfileAsync())["profile"]!["devices"]!.AsArray().Count == 0, "Media profile not configured");
    Check((await capabilities.GetResourcePolicyAsync())["policy"]!["reportOnly"]!.GetValue<bool>(), "Resource policy not configured");
    var page = await context.NewPageAsync();
    await page.SetContentAsync("<title>Mimic SDK</title><button onclick=\"this.textContent='done'\">run</button>");
    await page.GetByRole(Microsoft.Playwright.AriaRole.Button).ClickAsync();
    Check(await page.GetByRole(Microsoft.Playwright.AriaRole.Button).TextContentAsync() == "done", "Native Playwright migration scenario");
    var typedVersion = await playwright.Mimic.Commands.GetVersionAsync();
    Check(typedVersion.Version == playwright.Runtime.Identity["version"]!.GetValue<string>(), "Typed version response");
    try { await playwright.Mimic.Experimental.SendAsync("Mimic.futureUnsupported", new JsonObject { ["null"] = null }); throw new Exception("Unknown command accepted"); }
    catch (Mimic.Sdk.ProtocolException error) { Console.WriteLine($"Native browser-scope unknown method: {error.Code} {error.Message}"); Check(error.Code < 0 && error.Message.Length > 0, "Raw method error lost code"); }
    try { await playwright.Mimic.Commands.GenerateProfileAsync(new GenerateProfileParams { Seed = Optional<string>.Null() }); throw new Exception("Invalid null accepted"); }
    catch (Mimic.Sdk.ProtocolException error) { Check(error.Code == -32602 && error.DataValue is not null, "Typed call lost structured protocol error"); }
    await using (var attached = await PlaywrightSession.ConnectAsync(playwright.Runtime.Endpoint.ToString()))
    {
        var owned = await attached.NewContextAsync(); await owned.NewPageAsync();
        Check(attached.Browser is Microsoft.Playwright.IBrowser, "Adapter returned imitation browser");
    }
    Check(await page.TitleAsync() == "Mimic SDK", "Attached Playwright disposal affected existing client");
    await using (var puppeteer = await PuppeteerSession.ConnectAsync(playwright.Runtime.Endpoint.ToString()))
    {
        var nativeContext = await puppeteer.NewContextAsync(media: new JsonObject { ["devices"] = new JsonArray() });
        var nativePage = await nativeContext.NewPageAsync();
        await nativePage.SetContentAsync("<title>Puppeteer native</title><div id='value'>42</div>");
        Check(await nativePage.EvaluateExpressionAsync<string>("document.querySelector('#value').textContent") == "42", "Native PuppeteerSharp scenario");
        Check((await puppeteer.ForContext(nativeContext).GetMediaProfileAsync())["profile"]!["devices"]!.AsArray().Count == 0, "Puppeteer Context bridge");
    }
    Check(await page.TitleAsync() == "Mimic SDK", "Attached Puppeteer disposal affected existing client");
    if (args[0] == "--integration-candidate")
    {
        var configuration = JsonNode.Parse("{\"profile\":{\"generate\":{\"platform\":\"windows\",\"seed\":\"dotnet-sdk-profile\"}},\"media\":{\"devices\":[]}}")!.AsObject();
        var configured = await playwright.NewConfiguredContextAsync(configuration);
        Check(configured.Pages.Count == 0, "Configured context probe leaked");
        var configuredPage = await configured.NewPageAsync();
        Check(await configuredPage.EvaluateAsync<string>("navigator.platform") == "Win32", "Context profile did not reach native framework page");
        await using var native = await PuppeteerSession.ConnectAsync(playwright.Runtime.Endpoint.ToString());
        var nativeConfigured = await native.NewConfiguredContextAsync(configuration);
        var nativePage = await nativeConfigured.NewPageAsync();
        Check(await nativePage.EvaluateExpressionAsync<string>("navigator.platform") == "Win32", "Puppeteer configured profile did not apply");
        configuration.Remove("media");
        var factoryCalls = 0;
        async Task<MediaConfiguration> MediaFactory(ContextSetup setup, CancellationToken cancellationToken)
        {
            var sources = await setup.Mimic.Commands.GetMediaSourcesAsync(new() { BrowserContextId = Optional<string>.Of(setup.BrowserContextId) }, cancellationToken);
            Check(sources.Sources is not null, "Typed Context source discovery failed");
            factoryCalls++;
            return new() { Devices = Optional<List<MediaDeviceProfile>>.Of([]) };
        }
        var factoryContext = await playwright.NewConfiguredContextAsync(configuration, mediaFactory: MediaFactory);
        Check(factoryContext.Pages.Count == 0, "Media factory leaked probe page");
        Check(await (await factoryContext.NewPageAsync()).EvaluateAsync<string>("navigator.platform") == "Win32", "Playwright media factory broke managed profile");
        var sharpFactory = await native.NewConfiguredContextAsync(configuration, mediaFactory: MediaFactory);
        Check(await (await sharpFactory.NewPageAsync()).EvaluateExpressionAsync<string>("navigator.platform") == "Win32" && factoryCalls == 2, "Puppeteer media factory not applied");
        var contextCount = playwright.Browser.Contexts.Count;
        try { await playwright.NewConfiguredContextAsync(configuration, mediaFactory: (_, _) => Task.FromException<MediaConfiguration>(new InvalidOperationException("factory failure"))); throw new Exception("Factory error swallowed"); }
        catch (InvalidOperationException expected) { Check(expected.Message == "factory failure", "Factory exception changed"); }
        Check(playwright.Browser.Contexts.Count == contextCount, "Failed media factory leaked Context");
        var beforeClosingFactory = (await playwright.Mimic.SendAsync("Target.getBrowserContexts"))["browserContextIds"]!.ToJsonString();
        await using (var callbackSession = await PlaywrightSession.ConnectAsync(playwright.Runtime.Endpoint.ToString()))
        {
            try { await callbackSession.NewContextAsync(mediaFactory: async (_, _) => { await callbackSession.DisposeAsync(); return new() { Devices = Optional<List<MediaDeviceProfile>>.Of([]) }; }); throw new Exception("Closed callback session returned Context"); }
            catch (ObjectDisposedException) { }
        }
        await using (var callbackSession = await PuppeteerSession.ConnectAsync(playwright.Runtime.Endpoint.ToString()))
        {
            try { await callbackSession.NewContextAsync(mediaFactory: async (_, _) => { await callbackSession.DisposeAsync(); return new() { Devices = Optional<List<MediaDeviceProfile>>.Of([]) }; }); throw new Exception("Closed callback session returned Context"); }
            catch (ObjectDisposedException) { }
        }
        Check((await playwright.Mimic.SendAsync("Target.getBrowserContexts"))["browserContextIds"]!.ToJsonString() == beforeClosingFactory, "Closing factory leaked native Context");
    }
    var pid = playwright.Runtime.ProcessId!.Value;
    await playwright.DisposeAsync();
    Check(!Directory.Exists("/proc/" + pid), "Owned runtime survived disposal");
    var fixtureDirectory = Path.Combine(Path.GetTempPath(), "mimic-sdk-startup-" + Guid.NewGuid()); Directory.CreateDirectory(fixtureDirectory);
    try
    {
        var fake = Path.Combine(fixtureDirectory, "silent-runtime");
        var pidFile = Path.Combine(fixtureDirectory, "pid");
        await File.WriteAllTextAsync(fake, "#!/bin/sh\nprintf '%s' \"$$\" > '" + pidFile + "'\nexec sleep 60\n");
        if (OperatingSystem.IsLinux()) File.SetUnixFileMode(fake, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        try { await new RuntimeManager().LaunchAsync(new() { ExecutablePath = fake, AllowDownload = false, Timeout = TimeSpan.FromMilliseconds(200) }); throw new Exception("Silent runtime did not time out"); }
        catch (Mimic.Sdk.RuntimeException error) { Check(error.Kind == "startup", "Startup timeout classification"); }
        var failedPid = File.ReadAllText(pidFile); Check(!Directory.Exists("/proc/" + failedPid), "Timed-out child survived cleanup");
        using var cancelled = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        try { await new RuntimeManager().LaunchAsync(new() { ExecutablePath = fake, AllowDownload = false }, cancelled.Token); throw new Exception("Cancellation ignored"); }
        catch (OperationCanceledException) { }
        failedPid = File.ReadAllText(pidFile); Check(!Directory.Exists("/proc/" + failedPid), "Cancelled child survived cleanup");
    }
    finally { Directory.Delete(fixtureDirectory, true); }
    Console.WriteLine("PASS .NET real Playwright/PuppeteerSharp, typed/raw errors, Context configuration, attach isolation, owned teardown");
}
else if (args.Length == 3 && args[0] == "--install-offline")
{
    var options = new RuntimeOptions { RuntimeDirectory = args[1], ArchivePath = args[2], AllowDownload = false };
    var installed = await new RuntimeManager().InstallAsync(options);
    await RuntimeManager.VerifyAsync(installed.Directory!, installed.RuntimeLock, "linux-amd64");
    Console.WriteLine("PASS .NET verified offline archive/shared installation");
}
else if (args.Length == 2 && args[0] == "--install")
{
    Check(OperatingSystem.IsLinux(), "Qualification cache install is Linux-only");
    var manager = new RuntimeManager();
    var installation = await manager.InstallAsync(new() { RuntimeDirectory = args[1] });
    var cached = await manager.InstallAsync(new() { RuntimeDirectory = args[1], AllowDownload = false });
    Check(installation.ExecutablePath == cached.ExecutablePath, "Offline cache did not reuse artifact");
    await using var runtime = await manager.LaunchAsync(new() { RuntimeDirectory = args[1], AllowDownload = false });
    Check(runtime.Identity["version"]!.GetValue<string>() == installation.Release, "Installed runtime failed");
    try { RuntimeManager.Prune(installation.Directory!, new() { RuntimeDirectory = args[1] }); throw new Exception("Pruned leased runtime"); }
    catch (Mimic.Sdk.RuntimeException error) { Check(error.Kind == "lease", "Lease pruning error"); }
    Console.WriteLine("PASS .NET verified install, cached offline launch, shared receipt and active lease pruning guard");
}

sealed class CaptureTransport : IProtocolTransport
{
    public string? Method;
    public JsonObject? Parameters;
    public Task<JsonObject> SendAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
    { Method = method; Parameters = parameters?.DeepClone().AsObject(); return Task.FromResult(new JsonObject()); }
}
