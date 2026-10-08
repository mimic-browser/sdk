# Mimic for Rust

Native runtime installation, exact version pins, typed Mimic commands and an
optional adapter for the real `chromiumoxide::Browser` and `Page` types.

Install from crates.io with the optional chromiumoxide adapter:

```sh
cargo add mimic-browser --features chromiumoxide
```

For source development, from a parent directory containing a checkout named `sdk`:

```sh
cargo new mimic-rust-example
cd mimic-rust-example
cargo add mimic-browser --path ../sdk/rust --features chromiumoxide
```

The bundled v0.2.4 runtime includes the Frame security projection and Context
configuration bridge required by the native adapter. Default `RuntimeOptions`
downloads and verifies that exact release on first launch; no executable path
is required. Rust 1.89 or newer is required by the locked dependency graph.

```rust,no_run
use mimic_browser::{RuntimeOptions, generated::GetVersionParams};

# async fn example() -> Result<(), Box<dyn std::error::Error>> {
let mut session = mimic_browser::chromiumoxide::Session::launch(RuntimeOptions::default()).await?;
let page = session.browser.new_page("about:blank").await?;
page.goto("https://example.com").await?;
let title: String = page.evaluate("document.title").await?.into_value()?;
let version = session.mimic.get_version(GetVersionParams {}).await?;
println!("{title}: {}", version.version);
session.close().await?;
# Ok(()) }
```

Use `Session::connect(endpoint)` for an existing runtime. Closing an attached
session disconnects its sockets and disposes only Contexts created by that
session; it never closes the shared browser. `new_context(options)` returns the
framework's real `BrowserContextId`; pass that ID to native
`CreateTargetParams::builder().browser_context_id(id)` when creating a Page.

`new_context_with_media(options, factory)` discovers capture sources inside the
new Context before configuration and before its first Page. The factory receives
`ContextSetup { browser_context_id, mimic }` and returns a `MediaConfiguration`:

```rust,no_run
use mimic_browser::generated::{ConfigureContextParams, GetMediaSourcesParams, MediaConfiguration, WireOptional};
# async fn configure(session: &mut mimic_browser::chromiumoxide::Session) -> mimic_browser::Result<()> {
let context = session.new_context_with_media(
    ConfigureContextParams::default(),
    |setup| async move {
        let sources = setup.mimic.get_media_sources(GetMediaSourcesParams {
            browser_context_id: WireOptional::Value(setup.browser_context_id),
        }).await?;
        // Choose an available private source, then declare its public identity.
        let camera = sources.sources.iter().find(|source| source.kind == "videoinput")
            .ok_or_else(|| mimic_browser::Error::Invalid("No camera source available".into()))?;
        Ok(serde_json::from_value::<MediaConfiguration>(serde_json::json!({
            "devices": [{
                "key": "front", "kind": "videoinput", "label": "Studio Camera",
                "source": {"sourceId": camera.source_id},
                "modes": [{"width": 1280, "height": 720, "frameRate": 30}],
                "defaultMode": {"width": 1280, "height": 720, "frameRate": 30},
                "processing": {"resize": "crop-and-scale"}
            }]
        }))?)
    },
).await?;
# Ok(()) }
```

Private source IDs select capture backends. Public labels, keys, groups and modes
describe the devices visible to web content. Source discovery does not open a
capture stream. A failed factory disposes its newly created Context; passing
both `options.media` and a factory is rejected.
Media and resource settings alone preserve ordinary CDP emulation. Supplying an
explicit environment `profile` or `proxy` selects the managed configuration
bridge before the first Page.

`Client::call(method, params)` is the raw escape hatch. `None` omits the params
envelope, while `Some(serde_json::Value::Null)` preserves explicit null.
`client.experimental().domain("Mimic").call("newCommand", params)` uses the same
transport and does not probe or require generated membership. Protocol failures
preserve their full JSON payload, including omitted/null/arbitrary `data`.

Generated optional model fields use `WireOptional::Missing`, `Null` and
`Value(value)`. Install/network/process work begins only on an explicit
installation or launch call. Only current Windows amd64 and Linux amd64/glibc
2.39+ runtime artifacts are available. Explicit executable paths support local
runtime development. No Chromium download or framework launcher is used.

Run `cargo test` for local units and wire conformance. Browser integration tests
require `MIMIC_SDK_TEST_RUNTIME` and run only on Linux, without opening a visible
browser. Enable the framework feature with `cargo test --all-features`.
Set `MIMIC_SDK_TEST_ARCHIVE` to the retained official Linux release archive to
exercise installation and process leases, and optionally
`MIMIC_SDK_TEST_CACHE` for the shared cross-language cache. Rust 1.89+ is required
by the locked dependency graph.
The media integration test additionally requires `MIMIC_MEDIA_FIXTURE`, an
explicit synthetic-provider fixture executable; it never opens physical devices.

Unknown or live installer locks are never evicted merely because they are old.
`RuntimeManager::list()` and `verify(directory)` inspect receipts and executable
hashes without network access. `prune(&installation).await` removes only a
verified cache entry with no live or unverifiable leases, using the same
filesystem lock as launch. No pruning occurs automatically.
An interrupted lock whose owner cannot be proven absent needs explicit repair.
Call `close().await` to receive cleanup errors and confirm process exit; dropping
an owned process schedules termination/reaping on the active Tokio runtime.
