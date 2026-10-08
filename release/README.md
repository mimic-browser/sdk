# Independent SDK releases

`packages.json` owns each native package's version and source inputs. Package
versions are independent of Mimic runtime versions. Select changed packages
explicitly; language-only changes do not select other languages. A schema change
runs deterministic generation and all target checks, then releases only changed
packaged output. Docs alone never initiate publication.

See the [main README](../README.md) for the current published release and
installation status. This guide describes the release process. Source
installation and local packaging do not invoke a publisher.

The immutable default runtime is recorded in [`runtime-lock.json`](runtime-lock.json).
Registry publication requires explicit configuration and every selected adapter
to pass against that exact official executable. An unreleased development
runtime cannot satisfy the publication gate.

After a compatible official runtime is published, intentionally update all
packaged pins from its verified manifest and `SHA256SUMS`. Set `RUNTIME_VERSION`
to the exact published runtime tag selected for this SDK release:

```sh
python release/pin_runtime.py "$RUNTIME_VERSION"
```

The updater preserves the original manifest bytes and verifies both platform
archives against the published checksums. Build and qualify every affected
package against that exact runtime before publishing SDK packages. Pin updates
do not change native SDK versions or user lock files.

## Local preparation

```sh
python release/release.py plan --select node dotnet-core dotnet-playwright --output .build/plan.json
python release/release.py validate --plan .build/plan.json
python release/release.py build --plan .build/plan.json --output .build/release
```

When preparing a subsequent release, pass `--baseline previous-build.json` to
`plan`. Identical packaged inputs reject an empty bump; changed inputs require an
intentional version change in both native metadata and `packages.json`. The
planner does not edit versions, user locks or the packaged runtime lock.

Native tools must already be installed. Set `MIMIC_RELEASE_DOTNET`,
`MIMIC_RELEASE_MVN`, `MIMIC_RELEASE_PYTHON`, etc. to an executable path when using
portable tools; these values are never shell expressions. `build` uses native
packers, checks package envelopes and retains file/content hashes per artifact.
Completed packages are reused only after their bytes verify. A failed build's
private staging is removed; an unreceipted final directory requires inspection.

Qualify the selected build in Linux with a supplied, verified executable and the
original pinned release archive used by installer tests:

```sh
python release/qualify.py --build .build/release/build.json \
  --runtime /absolute/path/mimic --runtime-sha256 <sha256> \
  --archive /absolute/path/runtime-release.tar.gz \
  --output .build/release/qualification.json
```

Qualification records exact SDK/build/schema identities, raw runtime identity,
binary hash, official installer provenance, executed native checks and outcomes.
It binds the source inputs before and after testing and checks the runtime hash
again. Partial failures never produce a passing receipt. Test execution uses the
real native framework adapters, explicit raw transport, and headless loopback
runtime processes. Optional adapters have separate evidence and must not
be promoted merely by a modern adapter's passing result.

`sdk-checks.yml` compiles/tests all eight targets. `runtime-qualification.yml`
accepts an exact official runtime artifact, builds current packages and publishes
compatibility evidence as a workflow artifact. It has read-only repository
permissions and no registry credentials. It never edits package versions,
embedded pins or user locks, and never calls a publisher.

## Intentional publication and retries

`sdk-release.yml` is workflow-dispatch only. It reads an explicit reviewed plan,
verifies its input hashes, then binds it to the checkout commit: a committed plan
cannot contain its own commit hash. Building and qualification are separate from
the final publication step. Publication also requires the protected
`sdk-production` environment, `SDK_PUBLICATION_ENABLED=true`, explicit workflow
`publish=true`, `CI=true` and the publisher's `--execute` argument.

Use `release.py handoff` to inspect exact selected native commands without running
them. `publish.py` validates the clean committed checkout, unchanged artifacts,
plan and complete qualification. It retains per-package progress and verifies
downloaded registry artifacts after upload. Retry with the same build/progress
receipts; it first recovers already uploaded matching artifacts from the registry.
After a confirmed upload, verification waits for registry indexing for up to
10 minutes, or 35 minutes for Go's negative cache. Maven deployment processing
uses a separate 30-minute wait on the retained deployment ID. If a bound expires,
resume using the saved receipts. Authentication failures and byte mismatches
remain errors, never permission to overwrite or bump a version.

The workflow retains `.build/release`, including its frozen `plan.json`, native
packages and publication receipts. To resume, dispatch `sdk-release.yml` at the
same Git commit and with the same plan, setting `resume_run` to the completed
earlier run ID. The workflow verifies the repository, workflow and source commit
before downloading that run's artifact, then checks plan identity, package input
hashes, artifact bytes and retained state. Partial builds continue from their
verified artifacts. Native artifact paths must still resolve in the same hosted
checkout layout; resume never rewrites their identities or silently rebuilds a
partially published release.

If publication tooling needs repair after an upload, keep the original artifacts
and dispatch the repaired workflow with the same plan and `resume_run`, plus
`resume_source_revision` set to the original full SDK commit. This explicit
recovery requires a complete retained build and successful qualification. It
does not rebuild or requalify packages. The current selected package inputs,
schema and runtime lock must still match the original plan. GitHub run and exact
artifact provenance are authenticated before restore and again before publishing;
the original SDK revision remains in every original receipt. A separate
`recovery.json` records the tooling revision and its binding to the retained
plan, build and qualification. Later retries must use the same repaired tooling
commit and also validate that recovery lineage.
Source-producing Go, Cargo and Packagist publication cannot use this recovery.

| Registry      | Required CI configuration                                                                                                     | Publication identity                                                                               |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| npm           | GitHub trusted publisher for `sdk-release.yml` and environment `sdk-production`; `id-token: write`                            | `mimic-browser` native version                                                                     |
| PyPI          | GitHub trusted publisher for `sdk-release.yml` and environment `sdk-production`; `id-token: write`                            | `mimic-browser` wheel and sdist                                                                    |
| NuGet         | GitHub trusted publisher through `NuGet/login` (account `moreveal`)                                                           | Independently selected `mimic-browser`, `mimic-browser.Playwright`, `mimic-browser.PuppeteerSharp` |
| Maven Central | `MAVEN_CENTRAL_USERNAME`, `MAVEN_CENTRAL_PASSWORD`, `GPG_PRIVATE_KEY`, `GPG_PASSPHRASE` in `sdk-production`                   | `boo.mimic:mimic-browser`, JAR/sources/Javadoc/POM                                                 |
| Go            | Authenticated SDK origin with tag-write permission                                                                            | `go/vX.Y.Z` points at the exact SDK source commit                                                  |
| crates.io     | `CARGO_REGISTRY_TOKEN`                                                                                                        | `mimic-browser` crate; clean checkout and locked dependencies                                      |
| RubyGems      | GitHub trusted publisher through `rubygems/configure-rubygems-credentials`                                                    | `mimic-browser` gem                                                                                |
| Packagist     | `PHP_MIRROR_SSH_KEY` with a write-enabled deploy key only on the `mimic-browser/sdk-php` mirror; configured Packagist webhook | Exact packed PHP tree, ordinary `vX.Y.Z` tag; source and mirror commits retained                   |

PyPI authentication exchanges the GitHub OIDC identity for a short-lived upload
token immediately before publishing the selected Python package. No `PYPI_TOKEN`
secret is required. Other registry credentials are unchanged.

npm publication uses GitHub OIDC without an `NPM_TOKEN` secret or
`NODE_AUTH_TOKEN` fallback. A new npm trusted publisher must complete its first
successful publication within two days to validate its repository identity.

The PHP mirror contains no separately edited implementation. Only verified
`sdk/php` package contents enter it. Go/Packagist registries construct their own
ZIP containers, so verification compares complete canonical file content; native
uploaded packages require exact SHA256 bytes. NuGet.org adds a repository
signature: every original ZIP entry must remain byte-for-byte identical, with
only `.signature.p7s` added. The verifier checks the signature chain and an
advertised NuGet.org signing certificate using `dotnet nuget verify --all`.
Trusted fingerprints come from the official HTTPS service index, allowing
certificate rotation. Receipts preserve unsigned and downloaded archive hashes,
the exact payload digest and signature evidence. No force-push or version overwrite
is performed. Central deployment IDs are saved for retries; an unknown upload
outcome requires recovery of the existing deployment ID rather than a duplicate.

Configure registry credentials, noninteractive signing and mirror access before
enabling publication. The publisher requires those configurations and a
compatible officially released default runtime.
Maven Central requires verification of the `boo.mimic` namespace
by proving control of the `mimic.boo` domain. Before Maven upload, CI verifies a
signature from the dedicated key against an independently retrieved public key.
If the public key is absent, CI distributes only its public material through the
Ubuntu keyserver and verifies it again. A valid signing subkey is supported.
Packagist requires initial submission of the PHP mirror
and a configured webhook before its first version can be indexed.
Central bundle handling follows the official [Publisher API](https://central.sonatype.org/publish/publish-portal-api/)
and [artifact requirements](https://central.sonatype.org/publish/requirements/).

Synthetic capture is an explicit additional qualification. Supply both
`--media-fixture /absolute/path/to/synthetic-provider` and
`--media-fixture-sha256 <exact-sha256>` to `qualify.py` to run private-source/public
identity tests through every selected native client, including actual camera
pixels and microphone PCM. The fixture uses no physical devices. Its hash is
checked before and after the run. Without it, the receipt explicitly records
synthetic capture as `not-run`; ordinary runtime checks do not claim this proof.

The runtime repository's `qualify-sdk.yml` dispatches `runtime-released` only
after published Linux archive and executable hashes have been verified. The
runtime repository needs an explicitly configured `SDK_QUALIFICATION_TOKEN`
authorized for repository dispatch to this SDK repository; an absent token
produces a warning and a `not-configured` receipt. The SDK listener uses no
registry credentials and never changes versions or pins.
