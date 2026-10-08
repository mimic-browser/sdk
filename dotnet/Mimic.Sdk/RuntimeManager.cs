using System.Diagnostics;
using System.Formats.Tar;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Mimic.Sdk;

public sealed record RuntimeOptions
{
    public string? Version { get; init; }
    public string? LockFile { get; init; }
    public string? ExecutablePath { get; init; }
    public string? RuntimeDirectory { get; init; }
    public bool AllowDownload { get; init; } = true;
    public string? ArchivePath { get; init; }
    public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(60);
    public TimeSpan LockTimeout { get; init; } = TimeSpan.FromSeconds(120);
    public IReadOnlyList<string> Arguments { get; init; } = [];
}

public sealed class RuntimeException(string kind, string message, Exception? inner = null) : Exception(message, inner)
{
    public string Kind { get; } = kind;
}

public sealed record RuntimeInstallation(string ExecutablePath, string Release, string? Directory, JsonObject RuntimeLock);

/// <summary>Language-native implementation of spec/runtime-manager.md.</summary>
public sealed class RuntimeManager
{
    private static readonly HttpClient http = new() { Timeout = TimeSpan.FromMinutes(3) };
    private const string ReleaseBase = "https://github.com/mimic-browser/runtime/releases/download/";

    public static string NormalizeVersion(string value)
    {
        if (!Regex.IsMatch(value, @"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?$"))
            throw new RuntimeException("configuration", "Runtime version must be an exact semantic version, not a range or latest");
        return value.StartsWith('v') ? value : "v" + value;
    }

    public static string CacheRoot(RuntimeOptions? options = null)
    {
        var value = options?.RuntimeDirectory ?? Environment.GetEnvironmentVariable("MIMIC_RUNTIME_DIR");
        if (value is not null) return Path.GetFullPath(value);
        if (OperatingSystem.IsWindows())
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Mimic", "runtimes");
        return Path.Combine(Environment.GetEnvironmentVariable("XDG_CACHE_HOME") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".cache"), "Mimic", "runtimes");
    }

    public static string Platform()
    {
        if (RuntimeInformation.OSArchitecture != Architecture.X64) throw new RuntimeException("platform", "Mimic requires amd64");
        if (OperatingSystem.IsWindows()) return "windows-amd64";
        if (!OperatingSystem.IsLinux()) throw new RuntimeException("platform", "Mimic currently packages Windows and Linux amd64 only");
        try
        {
            var version = Marshal.PtrToStringAnsi(GlibcVersion())!;
            if (System.Version.Parse(version) < new System.Version(2, 39)) throw new RuntimeException("platform", "Mimic requires glibc 2.39 or newer");
        }
        catch (Exception error) when (error is DllNotFoundException or EntryPointNotFoundException)
        { throw new RuntimeException("platform", "Mimic requires glibc 2.39 or newer", error); }
        return "linux-amd64";
    }

    [DllImport("libc", EntryPoint = "gnu_get_libc_version")]
    private static extern IntPtr GlibcVersion();

    public static JsonObject DefaultLock()
    {
        using var source = typeof(RuntimeManager).Assembly.GetManifestResourceStream("Mimic.Sdk.runtime-lock.json")!;
        return JsonNode.Parse(source)!.AsObject();
    }

    public async Task<JsonObject> ResolveLockAsync(RuntimeOptions? options = null, CancellationToken cancellationToken = default)
    {
        options ??= new();
        var bundled = DefaultLock();
        JsonObject? requested = options.LockFile is null ? null : JsonNode.Parse(await File.ReadAllTextAsync(options.LockFile, cancellationToken))!.AsObject();
        var version = options.Version is not null ? NormalizeVersion(options.Version) : requested?["release"]?.GetValue<string>() is { } locked ? NormalizeVersion(locked)
            : Environment.GetEnvironmentVariable("MIMIC_RUNTIME_VERSION") is { } environment ? NormalizeVersion(environment) : bundled["release"]!.GetValue<string>();
        if (requested is not null && NormalizeVersion(requested["release"]!.GetValue<string>()) != version)
            throw new RuntimeException("configuration", "Explicit runtime version conflicts with lock file");
        if (requested is not null) { ValidateLock(requested, version); return requested; }
        if (bundled["release"]!.GetValue<string>() == version) { ValidateLock(bundled, version); return bundled; }
        var metadata = Path.Combine(CacheRoot(options), ".manifests", version + ".json");
        if (File.Exists(metadata))
        {
            var saved = JsonNode.Parse(await File.ReadAllTextAsync(metadata, cancellationToken))!.AsObject();
            ValidateLock(saved, version);
            return saved;
        }
        EnsureDownload(options, version);
        var baseUrl = ReleaseBase + version;
        var bytes = await http.GetByteArrayAsync(baseUrl + "/release-manifest.json", cancellationToken);
        var digest = Hex(SHA256.HashData(bytes));
        var sums = await http.GetStringAsync(baseUrl + "/SHA256SUMS", cancellationToken);
        if (!sums.Split('\n').Any(line => Regex.IsMatch(line.Trim(), "^" + digest + @"\s+\*?release-manifest\.json$", RegexOptions.IgnoreCase)))
            throw new RuntimeException("integrity", "Release manifest checksum is missing or incorrect");
        var result = new JsonObject { ["release"] = version, ["manifestSha256"] = digest, ["manifest"] = JsonNode.Parse(bytes), ["manifestJson"] = System.Text.Encoding.UTF8.GetString(bytes), ["baseUrl"] = baseUrl };
        ValidateLock(result, version);
        Directory.CreateDirectory(Path.GetDirectoryName(metadata)!);
        var temporary = metadata + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try { await File.WriteAllTextAsync(temporary, result.ToJsonString(), cancellationToken); File.Move(temporary, metadata, false); }
        catch (IOException) when (File.Exists(metadata))
        {
            var existing = JsonNode.Parse(await File.ReadAllTextAsync(metadata, cancellationToken))!.AsObject();
            if (!JsonNode.DeepEquals(existing, result)) throw new RuntimeException("integrity", "Cached release provenance conflicts with downloaded manifest");
        }
        finally { File.Delete(temporary); }
        return result;
    }

    public static void ValidateLock(JsonObject runtimeLock, string? selectedVersion = null)
    {
        var release = NormalizeVersion(runtimeLock["release"]?.GetValue<string>() ?? "");
        if (selectedVersion is not null && selectedVersion != release) throw new RuntimeException("configuration", "Lock release does not match selected runtime");
        var manifest = runtimeLock["manifest"]?.AsObject() ?? throw new RuntimeException("integrity", "Lock requires manifest");
        if (manifest["version"]?.GetValue<string>() != release) throw new RuntimeException("integrity", "Manifest release mismatch");
        ValidateHash(runtimeLock["manifestSha256"]?.GetValue<string>(), "manifest hash");
        if (!Regex.IsMatch(manifest["sourceRevision"]?.GetValue<string>() ?? "", "^[0-9a-f]{40}$")) throw new RuntimeException("integrity", "Invalid source revision");
        if (runtimeLock["baseUrl"]?.GetValue<string>() != ReleaseBase + release) throw new RuntimeException("integrity", "Manifest base URL is not the selected official release");
        if (runtimeLock["manifestJson"] is { } encoded)
        {
            var bytes = System.Text.Encoding.UTF8.GetBytes(encoded.GetValue<string>());
            if (Hex(SHA256.HashData(bytes)) != runtimeLock["manifestSha256"]!.GetValue<string>() || !JsonNode.DeepEquals(JsonNode.Parse(bytes), manifest))
                throw new RuntimeException("integrity", "Manifest bytes do not match locked digest and document");
        }
        else throw new RuntimeException("integrity", "Lock requires exact manifestJson for digest verification");
        var platforms = new HashSet<string>();
        foreach (var artifact in manifest["artifacts"]!.AsArray())
        {
            var platform = artifact!["platform"]!.GetValue<string>();
            if (!platforms.Add(platform) || platform is not ("windows-amd64" or "linux-amd64")) throw new RuntimeException("integrity", "Invalid or duplicate artifact platform");
            var expected = $"mimic-{release}-{platform}" + (platform.StartsWith("windows") ? ".zip" : ".tar.gz");
            if (artifact["archive"]!.GetValue<string>() != expected || artifact["size"]!.GetValue<long>() <= 0) throw new RuntimeException("integrity", "Invalid release artifact");
            ValidateHash(artifact["sha256"]?.GetValue<string>(), "archive hash");
            ValidateHash(artifact["binarySha256"]?.GetValue<string>(), "binary hash");
        }
    }

    public async Task<RuntimeInstallation> InstallAsync(RuntimeOptions? options = null, CancellationToken cancellationToken = default)
    {
        options ??= new();
        var platform = Platform();
        var executable = options.ExecutablePath ?? Environment.GetEnvironmentVariable("MIMIC_EXECUTABLE_PATH");
        var runtimeLock = executable is not null && options.LockFile is null ? DefaultLock() : await ResolveLockAsync(options, cancellationToken);
        var release = executable is not null && options.LockFile is null
            ? NormalizeVersion(options.Version ?? Environment.GetEnvironmentVariable("MIMIC_RUNTIME_VERSION") ?? runtimeLock["release"]!.GetValue<string>())
            : runtimeLock["release"]!.GetValue<string>();
        if (executable is not null)
        {
            executable = Path.GetFullPath(executable);
            if (!File.Exists(executable)) throw new RuntimeException("executable", "Explicit executable does not exist: " + executable);
            if (options.LockFile is not null)
            {
                var selectedArtifact = runtimeLock["manifest"]!["artifacts"]!.AsArray().Single(n => n!["platform"]!.GetValue<string>() == platform)!;
                if (await HashFileAsync(executable, cancellationToken) != selectedArtifact["binarySha256"]!.GetValue<string>()) throw new RuntimeException("integrity", "Explicit executable does not match locked artifact");
            }
            return new(executable, release, null, runtimeLock);
        }
        var manifest = runtimeLock["manifest"]!.AsObject();
        var artifact = manifest["artifacts"]!.AsArray().Select(n => n!.AsObject()).Single(n => n["platform"]!.GetValue<string>() == platform);
        var root = CacheRoot(options);
        var destination = Path.Combine(root, release, platform, artifact["binarySha256"]!.GetValue<string>());
        var basename = platform.StartsWith("windows") ? "mimic.exe" : "mimic";
        if (Directory.Exists(destination)) { await VerifyAsync(destination, runtimeLock, platform, cancellationToken); return new(Path.Combine(destination, basename), release, destination, runtimeLock); }
        if (options.ArchivePath is null) EnsureDownload(options, release);
        await using var installLock = await InstallLock.AcquireAsync(root, release + "-" + platform, options.LockTimeout, cancellationToken);
        if (Directory.Exists(destination)) { await VerifyAsync(destination, runtimeLock, platform, cancellationToken); return new(Path.Combine(destination, basename), release, destination, runtimeLock); }
        var staging = Path.Combine(root, ".staging", Guid.NewGuid().ToString());
        Directory.CreateDirectory(staging);
        try
        {
            var archive = Path.Combine(staging, "download");
            if (options.ArchivePath is not null)
            {
                if (new FileInfo(options.ArchivePath).Length != artifact["size"]!.GetValue<long>()) throw new RuntimeException("integrity", "Local archive size mismatch");
                await using var source = File.OpenRead(options.ArchivePath);
                await using var target = new FileStream(archive, FileMode.CreateNew);
                await source.CopyToAsync(target, cancellationToken);
            }
            else
            using (var response = await http.GetAsync(runtimeLock["baseUrl"]!.GetValue<string>() + "/" + artifact["archive"]!.GetValue<string>(), HttpCompletionOption.ResponseHeadersRead, cancellationToken))
            {
                response.EnsureSuccessStatusCode();
                await using var source = await response.Content.ReadAsStreamAsync(cancellationToken);
                await using var target = new FileStream(archive, FileMode.CreateNew);
                var buffer = new byte[64 * 1024];
                long total = 0;
                int count;
                while ((count = await source.ReadAsync(buffer, cancellationToken)) > 0)
                {
                    total += count;
                    if (total > artifact["size"]!.GetValue<long>()) throw new RuntimeException("integrity", "Archive exceeds declared size");
                    await target.WriteAsync(buffer.AsMemory(0, count), cancellationToken);
                }
                if (total != artifact["size"]!.GetValue<long>()) throw new RuntimeException("integrity", "Archive size mismatch");
            }
            if (await HashFileAsync(archive, cancellationToken) != artifact["sha256"]!.GetValue<string>()) throw new RuntimeException("integrity", "Archive checksum mismatch");
            var tree = Path.Combine(staging, "tree");
            Directory.CreateDirectory(tree);
            ExtractArchive(archive, tree, $"mimic-{release}-{platform}", platform.StartsWith("windows"));
            var binary = Path.Combine(tree, basename);
            if (!File.Exists(binary) || await HashFileAsync(binary, cancellationToken) != artifact["binarySha256"]!.GetValue<string>()) throw new RuntimeException("integrity", "Executable checksum mismatch");
            if (OperatingSystem.IsLinux()) File.SetUnixFileMode(binary, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute | UnixFileMode.GroupRead | UnixFileMode.GroupExecute | UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
            var receipt = Receipt(runtimeLock, platform, artifact);
            await File.WriteAllTextAsync(Path.Combine(tree, "installation.json"), receipt.ToJsonString(), cancellationToken);
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            Directory.Move(tree, destination);
            return new(Path.Combine(destination, basename), release, destination, runtimeLock);
        }
        finally { Directory.Delete(staging, true); }
    }

    public static void ExtractArchive(string archive, string destination, string topLevel, bool zip)
    {
        var seen = new HashSet<string>(OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
        string? SafeName(string name, bool directory)
        {
            name = name.Replace('\\', '/');
            if (name.StartsWith('/') || name.Contains(':') || name.Split('/').Any(p => p == "..")) throw new RuntimeException("integrity", "Unsafe archive path");
            name = name.TrimEnd('/');
            if (name == topLevel && directory) return null;
            if (name.StartsWith(topLevel + "/", StringComparison.Ordinal)) name = name[(topLevel.Length + 1)..];
            if (name.Length == 0 || !seen.Add(name)) throw new RuntimeException("integrity", "Duplicate or empty archive path");
            var result = Path.GetFullPath(Path.Combine(destination, name));
            if (!result.StartsWith(Path.GetFullPath(destination) + Path.DirectorySeparatorChar, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal)) throw new RuntimeException("integrity", "Archive path escapes staging");
            return result;
        }
        if (zip)
        {
            using var source = ZipFile.OpenRead(archive);
            foreach (var entry in source.Entries)
            {
                var mode = (entry.ExternalAttributes >> 16) & 0xF000;
                if (mode == 0xA000) throw new RuntimeException("integrity", "Archive symlinks are forbidden");
                var directory = entry.FullName.EndsWith('/');
                var path = SafeName(entry.FullName, directory);
                if (path is null) continue;
                if (directory) Directory.CreateDirectory(path);
                else { Directory.CreateDirectory(Path.GetDirectoryName(path)!); entry.ExtractToFile(path, false); }
            }
        }
        else
        {
            using var source = File.OpenRead(archive);
            using var gzip = new GZipStream(source, CompressionMode.Decompress);
            using var reader = new TarReader(gzip);
            TarEntry? entry;
            while ((entry = reader.GetNextEntry()) is not null)
            {
                if (entry.EntryType is not (TarEntryType.Directory or TarEntryType.RegularFile or TarEntryType.V7RegularFile)) throw new RuntimeException("integrity", "Archive links and special entries are forbidden");
                var directory = entry.EntryType == TarEntryType.Directory;
                var path = SafeName(entry.Name, directory);
                if (path is null) continue;
                if (directory) Directory.CreateDirectory(path);
                else { Directory.CreateDirectory(Path.GetDirectoryName(path)!); using var output = new FileStream(path, FileMode.CreateNew); entry.DataStream?.CopyTo(output); }
            }
        }
    }

    private static JsonObject Receipt(JsonObject runtimeLock, string platform, JsonObject artifact) => new()
    {
        ["release"] = runtimeLock["release"]!.DeepClone(), ["platform"] = platform,
        ["sourceRevision"] = runtimeLock["manifest"]!["sourceRevision"]!.DeepClone(),
        ["archiveSha256"] = artifact["sha256"]!.DeepClone(), ["binarySha256"] = artifact["binarySha256"]!.DeepClone(),
        ["executable"] = platform.StartsWith("windows") ? "mimic.exe" : "mimic", ["manifestSha256"] = runtimeLock["manifestSha256"]!.DeepClone()
    };

    public static async Task VerifyAsync(string directory, JsonObject runtimeLock, string platform, CancellationToken cancellationToken = default)
    {
        var artifact = runtimeLock["manifest"]!["artifacts"]!.AsArray().Select(n => n!.AsObject()).Single(n => n["platform"]!.GetValue<string>() == platform);
        var expected = Receipt(runtimeLock, platform, artifact);
        var receiptPath = Path.Combine(directory, "installation.json");
        if (!File.Exists(receiptPath)) throw new RuntimeException("integrity", "Incomplete installation: receipt is missing");
        var actual = JsonNode.Parse(await File.ReadAllTextAsync(receiptPath, cancellationToken))!.AsObject();
        foreach (var field in expected) if (!JsonNode.DeepEquals(actual[field.Key], field.Value)) throw new RuntimeException("integrity", "Installation provenance mismatch: " + field.Key);
        var binary = Path.Combine(directory, expected["executable"]!.GetValue<string>());
        if (!File.Exists(binary) || await HashFileAsync(binary, cancellationToken) != artifact["binarySha256"]!.GetValue<string>()) throw new RuntimeException("integrity", "Installed executable checksum mismatch");
    }

    public async Task<RuntimeProcess> LaunchAsync(RuntimeOptions? options = null, CancellationToken cancellationToken = default)
    {
        options ??= new();
        var installation = await InstallAsync(options, cancellationToken);
        if (installation.Directory is null) return await RuntimeProcess.StartAsync(installation, options, cancellationToken);
        var platform = Platform();
        await using var installLock = await InstallLock.AcquireAsync(CacheRoot(options), installation.Release + "-" + platform, options.LockTimeout, cancellationToken);
        await VerifyAsync(installation.Directory, installation.RuntimeLock, platform, cancellationToken);
        return await RuntimeProcess.StartAsync(installation, options, cancellationToken);
    }

    public static IReadOnlyList<string> List(RuntimeOptions? options = null)
    {
        var root = CacheRoot(options);
        return Directory.Exists(root) ? Directory.EnumerateFiles(root, "installation.json", SearchOption.AllDirectories).Select(Path.GetDirectoryName).OfType<string>().ToArray() : [];
    }

    public static void Prune(string directory, RuntimeOptions? options = null)
    {
        var root = Path.GetFullPath(CacheRoot(options)) + Path.DirectorySeparatorChar;
        var target = Path.GetFullPath(directory);
        if (!target.StartsWith(root, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal) || !File.Exists(Path.Combine(target, "installation.json")))
            throw new RuntimeException("configuration", "Prune only accepts a complete installation inside this cache");
        var receipt = JsonNode.Parse(File.ReadAllText(Path.Combine(target, "installation.json")))!.AsObject();
        var release = NormalizeVersion(receipt["release"]!.GetValue<string>());
        var platform = receipt["platform"]!.GetValue<string>();
        if (platform is not ("windows-amd64" or "linux-amd64")) throw new RuntimeException("integrity", "Invalid receipt platform");
        var installLock = InstallLock.AcquireAsync(CacheRoot(options), release + "-" + platform, options?.LockTimeout ?? TimeSpan.FromSeconds(120), CancellationToken.None).GetAwaiter().GetResult();
        try
        {
        var leases = Path.Combine(target, ".leases");
        if (Directory.Exists(leases) && Directory.EnumerateFileSystemEntries(leases).Any()) throw new RuntimeException("lease", "Installation has leases; inspect and repair stale leases explicitly before pruning");
        Directory.Delete(target, true);
        }
        finally { installLock.DisposeAsync().GetAwaiter().GetResult(); }
    }

    internal static async Task<string> HashFileAsync(string path, CancellationToken cancellationToken)
    { await using var input = File.OpenRead(path); return Hex(await SHA256.HashDataAsync(input, cancellationToken)); }
    private static string Hex(byte[] value) => Convert.ToHexString(value).ToLowerInvariant();
    private static void ValidateHash(string? value, string name) { if (!Regex.IsMatch(value ?? "", "^[0-9a-f]{64}$")) throw new RuntimeException("integrity", "Invalid " + name); }
    private static void EnsureDownload(RuntimeOptions options, string release)
    { if (!options.AllowDownload || Environment.GetEnvironmentVariable("MIMIC_DOWNLOAD") == "0") throw new RuntimeException("offline", $"Runtime {release} is not installed; enable downloads and call InstallAsync first"); }

    private sealed class InstallLock(string path, string token) : IAsyncDisposable
    {
        public static async Task<InstallLock> AcquireAsync(string root, string key, TimeSpan timeout, CancellationToken cancellationToken)
        {
            var path = Path.Combine(root, ".locks", key + ".lock");
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var deadline = Stopwatch.StartNew();
            var token = Guid.NewGuid().ToString();
            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();
                // Directory.CreateDirectory is not exclusive; native mkdir/CreateDirectory is.
                bool acquired = OperatingSystem.IsWindows() ? CreateDirectoryWindows(path, IntPtr.Zero) : Mkdir(path, 448) == 0;
                if (acquired)
                {
                    var owner = new JsonObject { ["pid"] = Environment.ProcessId, ["hostname"] = Environment.MachineName, ["token"] = token, ["createdAt"] = DateTimeOffset.UtcNow.ToString("O") };
                    try { await File.WriteAllTextAsync(Path.Combine(path, "owner.json"), owner.ToJsonString(), cancellationToken); }
                    catch { Directory.Delete(path, true); throw; }
                    return new(path, token);
                }
                if (!Directory.Exists(path)) throw new RuntimeException("permission", "Cannot create installation lock: " + path);
                if (deadline.Elapsed >= timeout) throw new RuntimeException("lock", "Installation lock timeout: " + path);
                await Task.Delay(100, cancellationToken);
            }
        }
        public ValueTask DisposeAsync()
        {
            var ownerPath = Path.Combine(path, "owner.json");
            if (File.Exists(ownerPath) && JsonNode.Parse(File.ReadAllText(ownerPath))?["token"]?.GetValue<string>() == token) Directory.Delete(path, true);
            return ValueTask.CompletedTask;
        }
        [DllImport("kernel32.dll", EntryPoint = "CreateDirectoryW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CreateDirectoryWindows(string path, IntPtr security);
        [DllImport("libc", EntryPoint = "mkdir", SetLastError = true)] private static extern int Mkdir(string path, uint mode);
    }
}
