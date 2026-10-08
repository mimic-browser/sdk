using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Mimic.Sdk;

public sealed class RuntimeProcess : IAsyncDisposable
{
    private readonly Process? process;
    private readonly string? lease;
    private readonly ProtocolConnection connection;
    private int disposed;
    public Uri Endpoint { get; }
    public Uri WebSocketEndpoint { get; }
    public JsonObject Identity { get; }
    public bool IsOwned => process is not null;
    public int? ProcessId => process?.Id;
    public IProtocolTransport Transport => connection;

    private RuntimeProcess(Uri endpoint, Uri websocket, ProtocolConnection connection, JsonObject identity, Process? process, string? lease)
    { Endpoint = endpoint; WebSocketEndpoint = websocket; this.connection = connection; Identity = identity; this.process = process; this.lease = lease; }

    public static async Task<RuntimeProcess> ConnectAsync(string endpoint, CancellationToken cancellationToken = default)
    {
        var uri = new Uri(endpoint, UriKind.Absolute);
        var websocket = await DiscoverAsync(uri, cancellationToken);
        var connection = await ProtocolConnection.ConnectAsync(websocket, cancellationToken);
        try
        {
            var identity = await connection.SendAsync("Mimic.getVersion", cancellationToken: cancellationToken);
            ValidateIdentity(identity, null);
            return new(uri, websocket, connection, identity, null, null);
        }
        catch { await connection.DisposeAsync(); throw; }
    }

    internal static async Task<RuntimeProcess> StartAsync(RuntimeInstallation installation, RuntimeOptions options, CancellationToken cancellationToken)
    {
        using var startup = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        startup.CancelAfter(options.Timeout);
        var info = new ProcessStartInfo(installation.ExecutablePath) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var argument in new[] { "--browser-mode", "headless", "--listen", "127.0.0.1:0" }) info.ArgumentList.Add(argument);
        foreach (var argument in options.Arguments)
        {
            if (argument.StartsWith("--listen", StringComparison.Ordinal) || argument.StartsWith("-listen", StringComparison.Ordinal) || argument.StartsWith("--browser-mode", StringComparison.Ordinal) || argument.StartsWith("-browser-mode", StringComparison.Ordinal))
                throw new RuntimeException("configuration", "Runtime arguments cannot override loopback binding or headless mode");
            info.ArgumentList.Add(argument);
        }
        var child = Process.Start(info) ?? throw new RuntimeException("startup", "Could not start Mimic");
        ProtocolConnection? connection = null;
        string? lease = null;
        var diagnostic = new Queue<string>();
        var ready = new TaskCompletionSource<Uri>(TaskCreationOptions.RunContinuationsAsynchronously);
        async Task Drain(StreamReader stream, bool stdout)
        {
            while (await stream.ReadLineAsync() is { } line)
            {
                lock (diagnostic) { diagnostic.Enqueue(line.Length > 2048 ? line[..2048] : line); while (diagnostic.Count > 30) diagnostic.Dequeue(); }
                if (stdout && Regex.Match(line, @"^Mimic listening on (http://127\.0\.0\.1:[1-9]\d*)$") is { Success: true } match)
                    ready.TrySetResult(new Uri(match.Groups[1].Value));
            }
        }
        var readers = Task.WhenAll(Drain(child.StandardOutput, true), Drain(child.StandardError, false));
        _ = child.WaitForExitAsync().ContinueWith(_ => ready.TrySetException(new RuntimeException("startup", $"Mimic exited before readiness ({child.ExitCode})")), TaskScheduler.Default);
        try
        {
            var endpoint = await ready.Task.WaitAsync(startup.Token);
            var websocket = await DiscoverAsync(endpoint, startup.Token);
            connection = await ProtocolConnection.ConnectAsync(websocket, startup.Token);
            var identity = await connection.SendAsync("Mimic.getVersion", cancellationToken: startup.Token);
            var enforceVersion = installation.Directory is not null || options.Version is not null || options.LockFile is not null || Environment.GetEnvironmentVariable("MIMIC_RUNTIME_VERSION") is not null;
            ValidateIdentity(identity, enforceVersion ? installation.Release : null);
            if (installation.Directory is not null)
            {
                var directory = Path.Combine(installation.Directory, ".leases");
                Directory.CreateDirectory(directory);
                lease = Path.Combine(directory, Guid.NewGuid() + ".json");
                await File.WriteAllTextAsync(lease, new JsonObject { ["launcherPid"] = Environment.ProcessId, ["runtimePid"] = child.Id, ["hostname"] = Environment.MachineName, ["createdAt"] = DateTimeOffset.UtcNow.ToString("O") }.ToJsonString(), startup.Token);
            }
            return new(endpoint, websocket, connection, identity, child, lease);
        }
        catch (Exception error)
        {
            if (connection is not null) await connection.DisposeAsync();
            if (!child.HasExited) child.Kill(true);
            await child.WaitForExitAsync();
            await readers;
            if (lease is not null) File.Delete(lease);
            child.Dispose();
            if (error is OperationCanceledException && cancellationToken.IsCancellationRequested) throw;
            string output; lock (diagnostic) output = string.Join('\n', diagnostic);
            throw new RuntimeException("startup", "Mimic startup failed: " + error.Message + "\n" + output, error);
        }
    }

    private static void ValidateIdentity(JsonObject identity, string? expected)
    {
        var version = identity["version"]?.GetValue<string>();
        if (string.IsNullOrEmpty(version) || identity["chromeVersion"] is null || identity["baseProfile"] is null) throw new RuntimeException("identity", "Endpoint is not a compatible Mimic runtime");
        if (expected is not null && version != expected && "v" + version != expected) throw new RuntimeException("identity", $"Expected Mimic {expected}, received {version}");
    }

    private static async Task<Uri> DiscoverAsync(Uri endpoint, CancellationToken cancellationToken)
    {
        if (endpoint.Scheme is "ws" or "wss") return endpoint;
        if (endpoint.Scheme is not ("http" or "https")) throw new RuntimeException("configuration", "Endpoint must use HTTP(S) or WS(S)");
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        var version = JsonNode.Parse(await client.GetStringAsync(new Uri(endpoint, "/json/version"), cancellationToken))!.AsObject();
        var websocket = new Uri(version["webSocketDebuggerUrl"]!.GetValue<string>());
        if (websocket.Scheme is not ("ws" or "wss") || !string.Equals(websocket.Host, endpoint.Host, StringComparison.OrdinalIgnoreCase)) throw new RuntimeException("identity", "Discovery returned an unrelated websocket endpoint");
        return websocket;
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0) return;
        try
        {
            if (process is not null && !process.HasExited)
            {
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                try { await connection.SendAsync("Browser.close", cancellationToken: deadline.Token); }
                catch (Exception error) when (error is IOException or OperationCanceledException or System.Net.WebSockets.WebSocketException or ProtocolException) { }
                try { await process.WaitForExitAsync(deadline.Token); }
                catch (OperationCanceledException) { if (!process.HasExited) process.Kill(true); await process.WaitForExitAsync(); }
            }
        }
        finally
        {
            await connection.DisposeAsync();
            if (lease is not null && process is { HasExited: true }) File.Delete(lease);
            process?.Dispose();
        }
    }
}
