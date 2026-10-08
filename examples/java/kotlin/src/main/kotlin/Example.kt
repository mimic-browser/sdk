import io.mimicbrowser.sdk.Generated
import io.mimicbrowser.sdk.RuntimeOptions
import io.mimicbrowser.sdk.playwright.PlaywrightSession
import java.nio.file.Path

fun main(args: Array<String>) {
    val options = RuntimeOptions()
    if (args.isNotEmpty()) options.executablePath(Path.of(args[0])).allowDownload(false)
    PlaywrightSession.launch(options).use { session ->
        val context = session.newContext()
        val page = context.newPage()
        page.setContent("<h1>Kotlin using native Playwright Java</h1>")
        check(page.locator("h1").textContent() == "Kotlin using native Playwright Java")
        val version = session.mimic().commands().getVersion(Generated.GetVersionParams())
        println("PASS Kotlin native Java SDK interop: ${version.version}")
    }
}
