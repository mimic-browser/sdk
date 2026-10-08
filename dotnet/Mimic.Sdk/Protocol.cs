using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text.Json.Nodes;

namespace Mimic.Sdk;

public interface IProtocolTransport
{
    Task<JsonObject> SendAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default);
}

public sealed class ProtocolException(int code, string message, JsonNode? data) : Exception(message)
{
    public int Code { get; } = code;
    public JsonNode? DataValue { get; } = data?.DeepClone();
}

/// <summary>A single multiplexed CDP connection used by typed and experimental calls.</summary>
public sealed class ProtocolConnection : IProtocolTransport, IAsyncDisposable
{
    private readonly ClientWebSocket socket = new();
    private readonly SemaphoreSlim sends = new(1, 1);
    private sealed record Pending(string? SessionId, TaskCompletionSource<JsonObject> Completion);
    private readonly ConcurrentDictionary<long, Pending> pending = new();
    private readonly CancellationTokenSource lifetime = new();
    private Task reader = Task.CompletedTask;
    private long sequence;
    private int disposed;
    public event Action<JsonObject>? EventReceived;
    public TimeSpan CommandTimeout { get; set; } = TimeSpan.FromSeconds(30);

    public static async Task<ProtocolConnection> ConnectAsync(Uri endpoint, CancellationToken cancellationToken = default)
    {
        var connection = new ProtocolConnection();
        try
        {
            await connection.socket.ConnectAsync(endpoint, cancellationToken).ConfigureAwait(false);
            connection.reader = connection.ReadAsync();
            return connection;
        }
        catch { await connection.DisposeAsync(); throw; }
    }

    public Task<JsonObject> SendAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
        => SendScopedAsync(method, parameters, null, cancellationToken);

    public IProtocolTransport Session(string sessionId)
    {
        ArgumentException.ThrowIfNullOrEmpty(sessionId);
        return new BoundSession(this, sessionId);
    }

    private async Task<JsonObject> SendScopedAsync(string method, JsonObject? parameters, string? sessionId, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(disposed != 0, this);
        ArgumentException.ThrowIfNullOrEmpty(method);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lifetime.Token);
        deadline.CancelAfter(CommandTimeout);
        var id = Interlocked.Increment(ref sequence);
        var completion = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!pending.TryAdd(id, new(sessionId, completion))) throw new InvalidOperationException("Duplicate protocol request ID");
        try
        {
            var request = new JsonObject { ["id"] = id, ["method"] = method, ["params"] = parameters?.DeepClone() ?? new JsonObject() };
            if (sessionId is not null) request["sessionId"] = sessionId;
            var bytes = System.Text.Encoding.UTF8.GetBytes(request.ToJsonString());
            await sends.WaitAsync(deadline.Token).ConfigureAwait(false);
            try { await socket.SendAsync(bytes.AsMemory(), WebSocketMessageType.Text, true, deadline.Token).ConfigureAwait(false); }
            finally { sends.Release(); }
            return await completion.Task.WaitAsync(deadline.Token).ConfigureAwait(false);
        }
        finally { pending.TryRemove(id, out _); }
    }

    private async Task ReadAsync()
    {
        Exception failure = new IOException("CDP connection closed");
        try
        {
            var buffer = new byte[16 * 1024];
            while (!lifetime.IsCancellationRequested)
            {
                using var frame = new MemoryStream();
                ValueWebSocketReceiveResult received;
                do
                {
                    received = await socket.ReceiveAsync(buffer.AsMemory(), lifetime.Token).ConfigureAwait(false);
                    if (received.MessageType == WebSocketMessageType.Close) return;
                    if (received.MessageType != WebSocketMessageType.Text) throw new IOException("Expected a JSON text CDP frame");
                    frame.Write(buffer, 0, received.Count);
                    if (frame.Length > 64 * 1024 * 1024) throw new IOException("CDP frame exceeds 64 MiB");
                } while (!received.EndOfMessage);
                var message = JsonNode.Parse(frame.ToArray())?.AsObject() ?? throw new IOException("Invalid CDP JSON");
                if (message["id"] is not JsonNode idNode)
                {
                    // Consumer event handlers cannot disrupt response routing.
                    if (EventReceived is { } handlers)
                        foreach (Action<JsonObject> handler in handlers.GetInvocationList())
                            _ = Task.Run(() => handler(message));
                    continue;
                }
                var id = idNode.GetValue<long>();
                if (!pending.TryGetValue(id, out var request) || message["sessionId"]?.GetValue<string>() != request.SessionId) continue;
                if (!pending.TryRemove(id, out _)) continue;
                var completion = request.Completion;
                if (message["error"] is JsonObject error)
                    completion.TrySetException(new ProtocolException(error["code"]!.GetValue<int>(), error["message"]!.GetValue<string>(), error["data"]));
                else completion.TrySetResult(message["result"]?.AsObject() ?? new JsonObject());
            }
        }
        catch (Exception error) { failure = error; }
        finally
        {
            lifetime.Cancel();
            foreach (var entry in pending) entry.Value.Completion.TrySetException(failure);
            pending.Clear();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0) return;
        lifetime.Cancel();
        socket.Abort();
        await reader.ConfigureAwait(false);
        socket.Dispose();
        lifetime.Dispose();
    }

    private sealed class BoundSession(ProtocolConnection connection, string sessionId) : IProtocolTransport
    {
        public Task<JsonObject> SendAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
            => connection.SendScopedAsync(method, parameters, sessionId, cancellationToken);
    }
}
