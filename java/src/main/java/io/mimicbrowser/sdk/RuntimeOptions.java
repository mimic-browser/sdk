package io.mimicbrowser.sdk;

import java.nio.file.Path;
import java.time.Duration;
import java.util.List;

/** Explicit selectors; a new options object performs no IO. */
public final class RuntimeOptions {
    public String version;
    public Path lockFile;
    public Path executablePath;
    public Path runtimeDirectory;
    public Path archivePath;
    public boolean allowDownload = true;
    public Duration timeout = Duration.ofSeconds(60);
    public Duration lockTimeout = Duration.ofSeconds(120);
    public List<String> arguments = List.of();
    public RuntimeOptions version(String value) { version = value; return this; }
    public RuntimeOptions lockFile(Path value) { lockFile = value; return this; }
    public RuntimeOptions executablePath(Path value) { executablePath = value; return this; }
    public RuntimeOptions runtimeDirectory(Path value) { runtimeDirectory = value; return this; }
    public RuntimeOptions archivePath(Path value) { archivePath = value; return this; }
    public RuntimeOptions allowDownload(boolean value) { allowDownload = value; return this; }
    public RuntimeOptions timeout(Duration value) { timeout = value; return this; }
}
