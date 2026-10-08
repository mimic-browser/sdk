# Mimic for Ruby

Package `mimic-browser` requires Ruby 3.2+. Ferrum is optional; require
`mimic_sdk/ferrum` to select it. Core import is inert.

Install the SDK from RubyGems and the optional Ferrum client:

```sh
gem install mimic-browser --no-document
gem install ferrum --version 0.18.0 --no-document
```

The bundled v0.2.4 runtime includes the CSS and navigation lifecycle support
required by Ferrum. The first launch downloads and verifies that exact runtime;
later launches reuse the persistent cache. No executable path is required.

```ruby
require 'mimic_sdk/ferrum'

MimicSDK::Ferrum.launch do |session|
  context = session.new_context
  page = context.create_page
  page.go_to('https://example.com')
  puts page.title
  puts session.mimic.get_version.version
end
```

The returned browser, context and page are actual Ferrum objects. `connect`
attaches without installing or starting a runtime; closing it preserves the
external process and other clients. `launch` owns startup and bounded cleanup.
All launches are headless and bind an OS-assigned loopback port.
Ferrum uses a dedicated WebSocket for each Page. A reverse proxy must forward
both the browser endpoint and `/devtools/page/*` on the same origin.

`new_context(profile:, media:, resource_policy:, proxy:)` applies Mimic settings
before returning the native context. A managed profile uses the runtime's
configure-context bridge. `for_page(page)` reuses one page capability handle on
the owned raw connection. Call `handle.close` to detach it early; the native Page
remains open. Closing the Page or integration invalidates the handle and removes
its cache entry. Framework session IDs are never copied across sockets.

`media:` also accepts a callable. It receives
`ContextSetup(browser_context_id:, mimic:)` after native Context creation and
before configuration or any user Page. Discover sources in that Context:

```ruby
context = session.new_context(media: lambda do |setup|
  sources = setup.mimic.get_media_sources({ 'browserContextId' => setup.browser_context_id }).sources
  microphone = sources.find { |source| source.kind == 'audioinput' }
  raise 'No microphone source available' unless microphone
  { 'devices' => [{
    'key' => 'voice', 'kind' => 'audioinput', 'label' => 'Studio Microphone',
    'source' => { 'sourceId' => microphone.source_id }
  }] }
end)
```

Private source IDs select capture backends independently of public labels, keys,
groups and camera modes. Discovery does not open capture. Callbacks run outside
the adapter's ownership mutex; a failing callback disposes its new Context.
Media configuration alone keeps ordinary CDP emulation available; only an
explicit environment profile or proxy selects managed Context configuration.

Runtime and adapter methods expose explicit keyword parameters. Generated models
also expose named constructors, for example
`Generated::MediaConfiguration.new(devices: [])`, and documented field/result
types for editor completion. Typed capabilities use snake_case methods. `UNSET` omits a
value, while `nil` remains JSON null. Raw wire names retain their exact spelling:

```ruby
session.mimic.experimental.newContributorCommand({ 'enabled' => true })
session.mimic.experimental.call('newContributorCommand', { 'enabled' => true })
```

Inspection is inert and explicit `call` handles reserved names. The same
dispatcher serves generated and experimental calls. `ProtocolError` retains
`code`, `message`, and `data`; missing error data differs from explicit null.
`timeout:` and `cancelled: -> { ... }` are supported by raw calls. Cancellation
after dispatch does not imply rollback and does not cause automatic retries.

`RuntimeManager.new(runtime_version:, lock_file:, executable_path:, runtime_dir:,
allow_download:, archive_path:, startup_timeout:, lock_timeout:, cancelled:)` follows the shared
runtime contract. It provides `resolve_lock`, `install`, `launch`, `list`,
`verify` and explicit `prune`. The packaged pin is v0.2.4. Exact archives and
executables are hash-verified; all languages reuse the same OS cache and leases.
Use `mimic-sdk install --offline --archive /path/to/official.tar.gz`, `list`,
`verify`, or `lock` for deliberate cache operations.

Run `test/sdk_test.rb`, `test/transport_test.rb`, and `test/ferrum_test.rb` inside
Linux/WSL. Real fixture tests cover reverse-order concurrent replies, event order,
omission/null, structured errors, cancellation, disconnect, archive integrity,
native automation and owned/attached cleanup. Exact runtime/framework evidence
belongs in the repository's qualification receipts.
`test/media_test.rb` requires `MIMIC_MEDIA_FIXTURE` pointing to the explicit
synthetic-provider fixture. It verifies native capture and public identity
without opening physical devices.
