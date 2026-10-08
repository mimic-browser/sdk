# Independent SDK releases

`packages.json` owns each native package's version and source inputs. Package
versions are independent of Mimic runtime versions. Select changed packages
explicitly; language-only changes do not select other languages. A schema change
runs deterministic generation and all target checks, then releases only changed
packaged output. Docs alone never initiate publication.

SDK source is distributed from [GitHub](https://github.com/mimic-browser/sdk).
The native registries are not enabled yet. Source installation and local
packaging do not invoke any publisher; the registry workflow below is an
independent, explicit release operation.

Several native clients need runtime fixes missing from the immutable default v0.2.2.
**Publication remains blocked** until a compatible official runtime is released,
the default pin is intentionally updated, and every selected adapter passes
against that exact official executable. An unreleased development runtime cannot satisfy
the publication gate.

After a compatible official runtime is published, intentionally update all
packaged pins from its verified manifest and `SHA256SUMS`:

```sh
python release/pin_runtime.py v0.2.3
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
  --archive /absolute/path/mimic-v0.2.2-linux-amd64.tar.gz \
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
A registry's temporary indexing delay leaves the release incomplete. A byte
mismatch is an error, never permission to overwrite or bump a version.

Preserve `.build/release` and publication state as CI artifacts. Resume in the
same source checkout with the retained artifacts/receipts and toolchain paths.
Native artifact paths are absolute and must be remapped consistently when moving
the receipt directory; never silently rebuild a partially published release.

| Registry      | Required CI configuration                                                                                                          | Publication identity                                                             |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| npm           | npm trusted publishing or `NODE_AUTH_TOKEN` through setup-node                                                                     | `mimic-browser` native version                                                   |
| PyPI          | `TWINE_USERNAME=__token__`, `TWINE_PASSWORD`                                                                                       | `mimic-browser` wheel and sdist                                                  |
| NuGet         | `NUGET_API_KEY`                                                                                                                    | Independently selected `Mimic.Sdk`, `Mimic.Playwright`, `Mimic.PuppeteerSharp`   |
| Maven Central | Portal base64 token in `MAVEN_CENTRAL_TOKEN`; pre-provisioned noninteractive GPG signing key                                       | `io.mimicbrowser:mimic-sdk`, JAR/sources/Javadoc/POM                             |
| Go            | Authenticated SDK origin with tag-write permission                                                                                 | `go/vX.Y.Z` points at the exact SDK source commit                                |
| crates.io     | `CARGO_REGISTRY_TOKEN`                                                                                                             | `mimic-sdk` crate; clean checkout and locked dependencies                        |
| RubyGems      | `GEM_HOST_API_KEY`                                                                                                                 | `mimic-browser-sdk` gem                                                          |
| Packagist     | `PHP_MIRROR_TOKEN` with Contents write permission only on the CI-only `mimic-browser/sdk-php` mirror; configured Packagist webhook | Exact packed PHP tree, ordinary `vX.Y.Z` tag; source and mirror commits retained |

The PHP mirror contains no separately edited implementation. Only verified
`sdk/php` package contents enter it. Go/Packagist registries construct their own
ZIP containers, so verification compares complete canonical file content; native
uploaded packages require exact SHA256 bytes. No force-push or version overwrite
is performed. Central deployment IDs are saved for retries; an unknown upload
outcome requires recovery of the existing deployment ID rather than a duplicate.

Configure registry credentials, noninteractive signing and mirror access before
enabling publication. The publisher requires those configurations and a
compatible officially released default runtime.
Maven Central also requires verification of the `io.mimicbrowser` namespace;
the repository's GitHub organization alone does not prove control of the
corresponding domain. Packagist requires initial submission of the PHP mirror
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
