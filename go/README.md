# Mimic for Go

The module is `github.com/mimic-browser/sdk/go` and the core package is `mimic`.
Import `/rod` or `/chromedp` to choose a real framework.

Install from the GitHub source repository. No versioned module tag is required
when using the checkout as a source replacement:

```sh
git clone https://github.com/mimic-browser/sdk.git
mkdir mimic-go-example
cd mimic-go-example
go mod init example.com/mimic-go-example
go mod edit -replace=github.com/mimic-browser/sdk/go=../sdk/go
go get github.com/mimic-browser/sdk/go/rod
```

An existing Go project can instead use
`go get github.com/mimic-browser/sdk/go/rod@main` directly. The runtime installer
uses the bundled default pin without an explicit executable path.

```go
ctx := context.Background()
session, err := rod.Launch(ctx, mimic.RuntimeOptions{})
if err != nil { return err }
defer session.Close()
browser, err := session.NewContext(ctx, nil, nil)
if err != nil { return err }
page, err := browser.Page(proto.TargetCreateTarget{URL: "https://example.com"})
if err != nil { return err }
value, err := page.Eval(`() => document.title`)
```

`Session.Browser` and returned objects are native Rod types. chromedp exposes
`Session.Context` and `Session.Browser`; pass the context to normal
`chromedp.Run` actions. Its existing-target attachment hydrates an already loaded
blank frame through public chromedp APIs. The chromedp integration requires
CSS, DOM refresh and numeric connection-ID fixes absent from the bundled v0.2.2
runtime. Use a compatible development executable through `RuntimeOptions` until
those fixes are available in an official release.

`Connect(ctx, endpoint)` does not install or spawn. Its `Close` cleans owned
contexts and connections, preserving the external runtime. `Launch` owns and
stops its process. Cancelling its parent context also closes that owned process.
Runtime execution always uses headless loopback mode.

Generated methods accept Go structs and `Optional[T]`. `Some(false)` retains
false; `Null[T]()` retains JSON null; the zero optional value omits a field.
The explicit raw path does not gate commands on schema membership:

```go
result, err := session.Mimic.Experimental.Call(ctx, "newContributorCommand",
    map[string]any{"enabled": true})
```

Use `mimic.Omitted` to omit raw `params`; `nil` sends JSON null. Errors retain
`*mimic.RPCError` code/message/data. A call deadline never implies rollback or
automatic retry. `ForPage` binds extensions to the correct connection/session.

Use `rod.Session.NewConfiguredContextWithMedia` or chromedp's
`LaunchConfiguredWithMedia` / `ConnectConfiguredWithMedia` when capture sources
must be chosen inside the new Context. They receive a `mimic.MediaFactory`:

```go
factory := func(ctx context.Context, setup mimic.ContextSetup) (mimic.MediaConfiguration, error) {
    sources, err := setup.Mimic.GetMediaSources(ctx, mimic.GetMediaSourcesParams{
        BrowserContextId: mimic.Some(setup.BrowserContextID),
    })
    if err != nil { return mimic.MediaConfiguration{}, err }
    for _, source := range sources.Sources {
        if source.Kind != "audioinput" { continue }
        selector, err := json.Marshal(map[string]string{"sourceId": source.SourceId})
        if err != nil { return mimic.MediaConfiguration{}, err }
        return mimic.MediaConfiguration{Devices: mimic.Some([]mimic.MediaDeviceProfile{
            {Key: "voice", Kind: "audioinput", Label: "Studio Microphone", Source: selector},
        })}, nil
    }
    return mimic.MediaConfiguration{}, fmt.Errorf("no microphone source is available")
}
browser, err := session.NewConfiguredContextWithMedia(ctx, mimic.ConfigureContextParams{}, factory)
```

The factory runs before any user Page and without an adapter ownership mutex.
Private `sourceId` values select capture backends; public labels, keys, groups
and camera modes describe what web content observes. Source discovery does not
open a capture stream. Failed factories dispose their new Contexts. Supplying
both a factory and `configuration.Media` is rejected.

The native `RuntimeManager` provides `ResolveLock`, `Install`, `Launch`, `List`,
`Verify` and explicit `Prune`. Use `go run ./cmd/mimic-sdk install --offline
--archive /path/to/official.tar.gz` for verified preinstallation, or the `lock`,
`list`, `verify`, `prune` commands. All managers share the common cache contract.
An explicit executable with a lock must match its hash before being started.

Go 1.26 is used by the current native client dependencies. Rod v0.116.2 and
chromedp v0.15.1 are the selected versions. No import starts a process or
downloads a browser. Run listener/integration tests in Linux/WSL with
`MIMIC_SDK_TEST_RUNTIME` and optionally `MIMIC_SDK_TEST_ARCHIVE` set. Generated
wire, extraction, integrity, live-lease, raw routing and ownership cases are
included; see the root qualification record for exact evidence.
Set `MIMIC_MEDIA_FIXTURE` to the explicit synthetic-provider fixture executable
to test both native media adapters. These tests never open physical devices.
