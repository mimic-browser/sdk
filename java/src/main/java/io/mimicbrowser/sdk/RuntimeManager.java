package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream;
import org.apache.commons.compress.archivers.zip.ZipArchiveEntry;
import org.apache.commons.compress.archivers.zip.ZipFile;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.zip.GZIPInputStream;

/** Verified native installer, sharing cache/lock/receipt layout with all SDKs. */
public final class RuntimeManager {
    private static final String RELEASE_BASE = "https://github.com/mimic-browser/runtime/releases/download/";
    private static final HttpClient HTTP = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NORMAL).connectTimeout(Duration.ofSeconds(30)).build();
    public record Installation(Path executablePath, String release, Path directory, JsonObject runtimeLock) {}
    public static String normalizeVersion(String value) {
        if (value == null || !value.matches("v?(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)(?:-beta\\.(0|[1-9]\\d*))?"))
            throw new SdkException("configuration", "Runtime version must be an exact semantic version, not a range or latest");
        return value.startsWith("v") ? value : "v" + value;
    }
    public static Path cacheRoot(RuntimeOptions options) {
        if (options != null && options.runtimeDirectory != null) return options.runtimeDirectory.toAbsolutePath().normalize();
        if (System.getenv("MIMIC_RUNTIME_DIR") != null) return Path.of(System.getenv("MIMIC_RUNTIME_DIR")).toAbsolutePath().normalize();
        if (windows()) {
            var local = System.getenv("LOCALAPPDATA");
            if (local == null) local = Path.of(System.getProperty("user.home"), "AppData", "Local").toString();
            return Path.of(local, "Mimic", "runtimes");
        }
        var cache = System.getenv("XDG_CACHE_HOME");
        return (cache == null ? Path.of(System.getProperty("user.home"), ".cache") : Path.of(cache)).resolve("Mimic/runtimes");
    }
    static boolean windows() { return System.getProperty("os.name").toLowerCase(Locale.ROOT).startsWith("windows"); }
    public static String platform() {
        if (!List.of("amd64", "x86_64").contains(System.getProperty("os.arch"))) throw new SdkException("platform", "Mimic requires amd64");
        if (windows()) return "windows-amd64";
        if (!System.getProperty("os.name").equals("Linux")) throw new SdkException("platform", "Mimic packages Windows and Linux amd64 only");
        try {
            var process = new ProcessBuilder("getconf", "GNU_LIBC_VERSION").redirectErrorStream(true).start();
            if (!process.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) { process.destroyForcibly(); throw new SdkException("platform", "Cannot inspect host glibc"); }
            var version = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim().replace("glibc ", "").split("\\.");
            if (process.exitValue() != 0 || version.length < 2 || Integer.parseInt(version[0]) < 2 || Integer.parseInt(version[0]) == 2 && Integer.parseInt(version[1]) < 39)
                throw new SdkException("platform", "Mimic requires glibc 2.39 or newer");
        } catch (IOException | InterruptedException | NumberFormatException error) {
            if (error instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SdkException("platform", "Mimic requires glibc 2.39 or newer", error);
        }
        return "linux-amd64";
    }
    public static JsonObject defaultLock() {
        try (var source = RuntimeManager.class.getResourceAsStream("/runtime-lock.json")) {
            if (source == null) throw new SdkException("configuration", "Packaged runtime lock missing");
            return parse(source.readAllBytes());
        } catch (IOException error) { throw new SdkException("io", "Cannot read packaged runtime lock", error); }
    }
    public JsonObject resolveLock(RuntimeOptions options) {
        if (options == null) options = new RuntimeOptions();
        try {
            var bundled = defaultLock();
            var requested = options.lockFile == null ? null : parse(Files.readAllBytes(options.lockFile));
            var version = options.version != null ? normalizeVersion(options.version) : requested != null ? normalizeVersion(string(requested, "release"))
                : System.getenv("MIMIC_RUNTIME_VERSION") != null ? normalizeVersion(System.getenv("MIMIC_RUNTIME_VERSION")) : string(bundled, "release");
            if (requested != null) { validateLock(requested, version); return requested; }
            if (string(bundled, "release").equals(version)) { validateLock(bundled, version); return bundled; }
            var metadata = cacheRoot(options).resolve(".manifests").resolve(version + ".json");
            if (Files.exists(metadata)) { var cached = parse(Files.readAllBytes(metadata)); validateLock(cached, version); return cached; }
            ensureDownload(options, version);
            var base = RELEASE_BASE + version;
            var bytes = download(base + "/release-manifest.json");
            var digest = hash(bytes);
            var sums = new String(download(base + "/SHA256SUMS"), StandardCharsets.UTF_8);
            if (!sums.lines().anyMatch(line -> line.trim().matches("(?i)" + digest + "\\s+\\*?release-manifest\\.json"))) throw new SdkException("integrity", "Release manifest checksum missing or incorrect");
            var result = new JsonObject();
            result.addProperty("release", version); result.addProperty("manifestSha256", digest); result.add("manifest", parse(bytes));
            result.addProperty("manifestJson", new String(bytes, StandardCharsets.UTF_8)); result.addProperty("baseUrl", base);
            validateLock(result, version);
            Files.createDirectories(metadata.getParent());
            var temporary = Files.createTempFile(metadata.getParent(), ".manifest-", ".tmp");
            try {
                Files.writeString(temporary, result.toString());
                try { Files.createLink(metadata, temporary); }
                catch (FileAlreadyExistsException conflict) {
                    if (!parse(Files.readAllBytes(metadata)).equals(result)) throw new SdkException("integrity", "Cached provenance conflicts with downloaded release");
                }
            } finally { Files.deleteIfExists(temporary); }
            return result;
        } catch (IOException error) { throw new SdkException("io", "Cannot resolve runtime manifest", error); }
    }
    public static void validateLock(JsonObject lock, String version) {
        var release = normalizeVersion(string(lock, "release"));
        if (version != null && !release.equals(version)) throw new SdkException("configuration", "Explicit runtime version conflicts with lock file");
        var manifest = lock.getAsJsonObject("manifest");
        if (!string(manifest, "version").equals(release)) throw new SdkException("integrity", "Manifest release mismatch");
        if (!string(manifest, "sourceRevision").matches("[0-9a-f]{40}")) throw new SdkException("integrity", "Invalid source revision");
        validateHash(string(lock, "manifestSha256"));
        if (!string(lock, "baseUrl").equals(RELEASE_BASE + release)) throw new SdkException("integrity", "Manifest URL is not the selected official release");
        var bytes = string(lock, "manifestJson").getBytes(StandardCharsets.UTF_8);
        if (!hash(bytes).equals(string(lock, "manifestSha256")) || !parse(bytes).equals(manifest)) throw new SdkException("integrity", "Manifest bytes do not match locked digest and document");
        var platforms = new HashSet<String>();
        for (var value : manifest.getAsJsonArray("artifacts")) {
            var artifact = value.getAsJsonObject();
            var platform = string(artifact, "platform");
            if (!List.of("linux-amd64", "windows-amd64").contains(platform) || !platforms.add(platform)) throw new SdkException("integrity", "Invalid or duplicate artifact platform");
            var expected = "mimic-" + release + "-" + platform + (platform.startsWith("windows") ? ".zip" : ".tar.gz");
            if (!string(artifact, "archive").equals(expected) || artifact.get("size").getAsLong() <= 0) throw new SdkException("integrity", "Invalid artifact identity");
            validateHash(string(artifact, "sha256")); validateHash(string(artifact, "binarySha256"));
        }
    }
    public Installation install(RuntimeOptions options) {
        if (options == null) options = new RuntimeOptions();
        var platform = platform();
        var explicit = options.executablePath != null ? options.executablePath : System.getenv("MIMIC_EXECUTABLE_PATH") == null ? null : Path.of(System.getenv("MIMIC_EXECUTABLE_PATH"));
        var lock = explicit != null && options.lockFile == null ? defaultLock() : resolveLock(options);
        var release = explicit != null && options.lockFile == null ? normalizeVersion(options.version != null ? options.version : System.getenv("MIMIC_RUNTIME_VERSION") != null ? System.getenv("MIMIC_RUNTIME_VERSION") : string(lock, "release")) : string(lock, "release");
        var artifact = artifact(lock, platform);
        try {
            if (explicit != null) {
                explicit = explicit.toAbsolutePath().normalize();
                if (!Files.isRegularFile(explicit)) throw new SdkException("executable", "Explicit executable does not exist: " + explicit);
                if (options.lockFile != null && !hashFile(explicit).equals(string(artifact, "binarySha256"))) throw new SdkException("integrity", "Explicit executable does not match locked artifact");
                return new Installation(explicit, release, null, lock);
            }
            var root = cacheRoot(options);
            var destination = root.resolve(release).resolve(platform).resolve(string(artifact, "binarySha256"));
            var basename = platform.startsWith("windows") ? "mimic.exe" : "mimic";
            if (Files.exists(destination)) { verify(destination, lock, platform); return new Installation(destination.resolve(basename), release, destination, lock); }
            if (options.archivePath == null) ensureDownload(options, release);
            try (var installLock = InstallLock.acquire(root, release + "-" + platform, options.lockTimeout)) {
                if (Files.exists(destination)) { verify(destination, lock, platform); return new Installation(destination.resolve(basename), release, destination, lock); }
                var stagingRoot = root.resolve(".staging"); Files.createDirectories(stagingRoot);
                var staging = Files.createTempDirectory(stagingRoot, "install-");
                try {
                    var archive = staging.resolve("download");
                    if (options.archivePath == null) downloadFile(string(lock, "baseUrl") + "/" + string(artifact, "archive"), archive, artifact.get("size").getAsLong());
                    else {
                        if (Files.size(options.archivePath) != artifact.get("size").getAsLong()) throw new SdkException("integrity", "Local archive size mismatch");
                        Files.copy(options.archivePath, archive);
                    }
                    if (!hashFile(archive).equals(string(artifact, "sha256"))) throw new SdkException("integrity", "Archive checksum mismatch");
                    var tree = staging.resolve("tree"); Files.createDirectory(tree);
                    extractArchive(archive, tree, "mimic-" + release + "-" + platform, platform.startsWith("windows"));
                    var binary = tree.resolve(basename);
                    if (!Files.isRegularFile(binary) || !hashFile(binary).equals(string(artifact, "binarySha256"))) throw new SdkException("integrity", "Executable checksum mismatch");
                    if (!windows()) Files.setPosixFilePermissions(binary, PosixFilePermissions.fromString("rwxr-xr-x"));
                    Files.writeString(tree.resolve("installation.json"), receipt(lock, platform).toString(), StandardOpenOption.CREATE_NEW);
                    Files.createDirectories(destination.getParent());
                    Files.move(tree, destination, StandardCopyOption.ATOMIC_MOVE);
                    return new Installation(destination.resolve(basename), release, destination, lock);
                } finally { deleteTree(staging); }
            }
        } catch (IOException error) { throw new SdkException("io", "Runtime installation failed", error); }
    }
    public RuntimeProcess launch(RuntimeOptions options) {
        var selected = options == null ? new RuntimeOptions() : options;
        var installation = install(selected);
        if (installation.directory() == null) return RuntimeProcess.start(installation, selected);
        var platform = platform();
        try (var installLock = InstallLock.acquire(cacheRoot(selected), installation.release() + "-" + platform, selected.lockTimeout)) {
            verify(installation.directory(), installation.runtimeLock(), platform);
            return RuntimeProcess.start(installation, selected);
        } catch (IOException error) { throw new SdkException("lock", "Cannot acquire runtime launch lease", error); }
    }
    public static void verify(Path directory, JsonObject lock, String platform) {
        try {
            var receipt = directory.resolve("installation.json");
            if (!Files.isRegularFile(receipt)) throw new SdkException("integrity", "Incomplete installation: receipt missing");
            var actual = parse(Files.readAllBytes(receipt));
            var expected = receipt(lock, platform);
            for (var field : expected.entrySet()) if (!field.getValue().equals(actual.get(field.getKey()))) throw new SdkException("integrity", "Installation provenance mismatch: " + field.getKey());
            var binary = directory.resolve(string(expected, "executable"));
            if (!Files.isRegularFile(binary) || !hashFile(binary).equals(string(expected, "binarySha256"))) throw new SdkException("integrity", "Installed executable checksum mismatch");
        } catch (IOException error) { throw new SdkException("integrity", "Cannot verify installation", error); }
    }
    public static List<Path> list(RuntimeOptions options) {
        var root = cacheRoot(options);
        if (!Files.exists(root)) return List.of();
        try (var files = Files.walk(root)) { return files.filter(path -> path.getFileName().toString().equals("installation.json")).map(Path::getParent).toList(); }
        catch (IOException error) { throw new SdkException("io", "Cannot list runtime installations", error); }
    }
    public static void prune(Path directory, RuntimeOptions options) {
        var root = cacheRoot(options).toAbsolutePath().normalize();
        var target = directory.toAbsolutePath().normalize();
        if (!target.startsWith(root) || target.equals(root) || !Files.isRegularFile(target.resolve("installation.json"))) throw new SdkException("configuration", "Prune only accepts a complete installation inside this cache");
        try {
            var receipt = parse(Files.readAllBytes(target.resolve("installation.json")));
            var release = normalizeVersion(string(receipt, "release"));
            var platform = string(receipt, "platform");
            if (!List.of("windows-amd64", "linux-amd64").contains(platform)) throw new SdkException("integrity", "Invalid receipt platform");
            try (var installLock = InstallLock.acquire(root, release + "-" + platform, options == null ? Duration.ofSeconds(120) : options.lockTimeout)) {
            var leases = target.resolve(".leases");
            if (Files.exists(leases)) try (var files = Files.list(leases)) { if (files.findAny().isPresent()) throw new SdkException("lease", "Installation has leases; inspect and repair stale leases explicitly"); }
            deleteTree(target);
            }
        } catch (IOException error) { throw new SdkException("io", "Cannot prune installation", error); }
    }
    public static void extractArchive(Path archive, Path destination, String topLevel, boolean zip) throws IOException {
        var seen = new HashSet<String>();
        if (zip) {
            try (var source = ZipFile.builder().setPath(archive).get()) {
                var entries = source.getEntries();
                while (entries.hasMoreElements()) {
                    var entry = entries.nextElement();
                    if (entry.isUnixSymlink()) throw new SdkException("integrity", "Archive symlinks are forbidden");
                    var path = safeArchivePath(entry.getName(), entry.isDirectory(), destination, topLevel, seen);
                    if (path == null) continue;
                    if (entry.isDirectory()) Files.createDirectories(path);
                    else { Files.createDirectories(path.getParent()); try (var input = source.getInputStream(entry)) { Files.copy(input, path); } }
                }
            }
        } else {
            try (var input = new TarArchiveInputStream(new GZIPInputStream(Files.newInputStream(archive)))) {
                TarArchiveEntry entry;
                while ((entry = input.getNextEntry()) != null) {
                    if (!entry.isFile() && !entry.isDirectory() || entry.isSymbolicLink() || entry.isLink()) throw new SdkException("integrity", "Archive links and special entries are forbidden");
                    var path = safeArchivePath(entry.getName(), entry.isDirectory(), destination, topLevel, seen);
                    if (path == null) continue;
                    if (entry.isDirectory()) Files.createDirectories(path);
                    else { Files.createDirectories(path.getParent()); Files.copy(input, path); }
                }
            }
        }
    }
    private static Path safeArchivePath(String name, boolean directory, Path destination, String topLevel, HashSet<String> seen) {
        name = name.replace('\\', '/').replaceAll("/+$", "");
        if (name.startsWith("/") || name.contains(":") || List.of(name.split("/")).contains("..")) throw new SdkException("integrity", "Unsafe archive path");
        if (name.equals(topLevel) && directory) return null;
        if (name.startsWith(topLevel + "/")) name = name.substring(topLevel.length() + 1);
        if (name.isEmpty() || !seen.add(windows() ? name.toLowerCase(Locale.ROOT) : name)) throw new SdkException("integrity", "Duplicate or empty archive entry");
        var path = destination.resolve(name).toAbsolutePath().normalize();
        if (!path.startsWith(destination.toAbsolutePath().normalize()) || path.equals(destination.toAbsolutePath().normalize())) throw new SdkException("integrity", "Archive path escapes staging");
        return path;
    }
    private static JsonObject receipt(JsonObject lock, String platform) {
        var artifact = artifact(lock, platform);
        var result = new JsonObject();
        result.addProperty("release", string(lock, "release")); result.addProperty("platform", platform);
        result.addProperty("sourceRevision", string(lock.getAsJsonObject("manifest"), "sourceRevision"));
        result.addProperty("archiveSha256", string(artifact, "sha256")); result.addProperty("binarySha256", string(artifact, "binarySha256"));
        result.addProperty("executable", platform.startsWith("windows") ? "mimic.exe" : "mimic"); result.addProperty("manifestSha256", string(lock, "manifestSha256"));
        return result;
    }
    private static JsonObject artifact(JsonObject lock, String platform) {
        for (var value : lock.getAsJsonObject("manifest").getAsJsonArray("artifacts")) if (string(value.getAsJsonObject(), "platform").equals(platform)) return value.getAsJsonObject();
        throw new SdkException("platform", "Release has no artifact for " + platform);
    }
    static String string(JsonObject value, String field) {
        if (value == null || !value.has(field) || !value.get(field).isJsonPrimitive() || !value.getAsJsonPrimitive(field).isString()) throw new SdkException("integrity", "Expected string field: " + field);
        return value.get(field).getAsString();
    }
    static JsonObject parse(byte[] bytes) { return JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject(); }
    private static void validateHash(String value) { if (!value.matches("[0-9a-f]{64}")) throw new SdkException("integrity", "Invalid SHA256 digest"); }
    static String hash(byte[] bytes) { try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); } catch (java.security.NoSuchAlgorithmException error) { throw new IllegalStateException(error); } }
    static String hashFile(Path path) throws IOException {
        try (var input = Files.newInputStream(path)) {
            var digest = MessageDigest.getInstance("SHA-256"); var buffer = new byte[65536]; int count;
            while ((count = input.read(buffer)) >= 0) { interrupted(); digest.update(buffer, 0, count); }
            return HexFormat.of().formatHex(digest.digest());
        } catch (java.security.NoSuchAlgorithmException error) { throw new IllegalStateException(error); }
    }
    private static void ensureDownload(RuntimeOptions options, String release) { if (!options.allowDownload || "0".equals(System.getenv("MIMIC_DOWNLOAD"))) throw new SdkException("offline", "Runtime " + release + " is missing; enable downloads and call install first"); }
    private static byte[] download(String url) {
        try {
            var response = HTTP.send(HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofMinutes(3)).GET().build(), HttpResponse.BodyHandlers.ofByteArray());
            if (response.statusCode() != 200) throw new SdkException("download", "HTTP " + response.statusCode() + " for " + url);
            if (response.body().length > 4 * 1024 * 1024) throw new SdkException("integrity", "Release metadata too large");
            return response.body();
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new SdkException("cancelled", "Download interrupted", error);
        } catch (IOException error) { throw new SdkException("download", "Download failed: " + url, error); }
    }
    private static void downloadFile(String url, Path destination, long size) throws IOException {
        try {
            var response = HTTP.send(HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofMinutes(3)).GET().build(), HttpResponse.BodyHandlers.ofInputStream());
            try (var input = response.body(); var output = Files.newOutputStream(destination, StandardOpenOption.CREATE_NEW)) {
                if (response.statusCode() != 200) throw new SdkException("download", "HTTP " + response.statusCode());
                var buffer = new byte[65536]; long total = 0; int count;
                while ((count = input.read(buffer)) >= 0) { interrupted(); total += count; if (total > size) throw new SdkException("integrity", "Archive exceeds declared size"); output.write(buffer, 0, count); }
                if (total != size) throw new SdkException("integrity", "Archive size mismatch");
            }
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new SdkException("cancelled", "Download interrupted", error); }
    }
    static void interrupted() { if (Thread.currentThread().isInterrupted()) throw new SdkException("cancelled", "Operation interrupted"); }
    static void deleteTree(Path path) throws IOException {
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return;
        try (var files = Files.walk(path)) { for (var entry : files.sorted(Comparator.reverseOrder()).toList()) Files.delete(entry); }
    }
    private static final class InstallLock implements AutoCloseable {
        final Path path; final String token;
        private InstallLock(Path path, String token) { this.path = path; this.token = token; }
        static InstallLock acquire(Path root, String key, Duration timeout) throws IOException {
            var path = root.resolve(".locks").resolve(key + ".lock"); Files.createDirectories(path.getParent());
            var token = UUID.randomUUID().toString(); long start = System.nanoTime();
            while (true) {
                interrupted();
                try {
                    Files.createDirectory(path);
                    var owner = new JsonObject(); owner.addProperty("pid", ProcessHandle.current().pid()); owner.addProperty("hostname", hostname());
                    owner.addProperty("token", token); owner.addProperty("createdAt", Instant.now().toString());
                    try { Files.writeString(path.resolve("owner.json"), owner.toString(), StandardOpenOption.CREATE_NEW); }
                    catch (IOException error) { deleteTree(path); throw error; }
                    return new InstallLock(path, token);
                } catch (FileAlreadyExistsException error) {
                    if (System.nanoTime() - start > timeout.toNanos()) throw new SdkException("lock", "Installation lock timeout: " + path);
                    try { Thread.sleep(100); } catch (InterruptedException cancelled) { Thread.currentThread().interrupt(); throw new SdkException("cancelled", "Installation lock wait interrupted", cancelled); }
                }
            }
        }
        @Override public void close() throws IOException {
            var owner = path.resolve("owner.json");
            if (Files.exists(owner) && string(parse(Files.readAllBytes(owner)), "token").equals(token)) deleteTree(path);
        }
    }
    static String hostname() {
        try { return java.net.InetAddress.getLocalHost().getHostName(); }
        catch (java.net.UnknownHostException error) { return System.getenv().getOrDefault("HOSTNAME", System.getenv().getOrDefault("COMPUTERNAME", "unknown")); }
    }
}
