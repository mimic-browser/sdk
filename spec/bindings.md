# Generated binding integration

`schema/mimic/protocol.json` owns the current Mimic extension wire contract.
The schema has no compatibility mode or contract version selector. Runtime
release versions and schema SHA-256 hashes identify implementation and provenance.

`python generator/generate.py` writes eight deterministic language projections.
`--check` verifies committed output without changing it. No network is used.

Each command has `<Command>Params` and `<Command>Result` models, for example
`CreateContextParams` and `CreateContextResult`. Generated constants preserve
the exact `Mimic.createContext` wire method. Session identity belongs to the
handwritten, explicitly bound transport, never to an ambient current page.
Stable typed calls use these models; raw/experimental calls bypass generated
membership and send the user's exact method and JSON unchanged.

| Target | Generated output and integration                                                                           |
| ------ | ---------------------------------------------------------------------------------------------------------- |
| Node   | `src/generated.ts`: exported interfaces and `CommandMap` keyed by exact method                             |
| Python | `mimic/generated.py`: dataclasses with snake_case properties, `UNSET`, recursive `to_wire` and `from_wire` |
| .NET   | `Mimic.Sdk/Generated.cs`: `Mimic.Sdk.Generated` models with PascalCase properties and `JsonPropertyName`   |
| Java   | `io.mimicbrowser.sdk.Generated`: nested public model classes with camelCase fields, compatible with Gson   |
| Go     | `generated.go`: package `mimic`, exported structs with JSON tags                                           |
| Rust   | `src/generated.rs`: serde models, snake_case properties with exact wire renames                            |
| Ruby   | `lib/mimic_sdk/generated.rb`: `MimicSDK::Generated` models with snake_case attributes and `to_wire`        |
| PHP    | `src/Generated.php`: `Mimic\Sdk\Generated` model classes and `JsonSerializable`                            |

Optional is distinct from nullable. A missing input field preserves the runtime
default. Explicit null is not converted into omission. Nullable result fields
retain null. Generic JSON is reserved for genuinely open diagnostic payloads,
JSON Schema documents and explicit union escape representations in languages
without structural unions. Snapshot file bytes remain base64 strings on wire.

The runtime semantic support registry remains authoritative. Generated shape
coverage is not an implementation or browser compatibility claim. There are
currently no Mimic-domain events; normal CDP events stay owned by the chosen
automation framework and are preserved by the raw transport.
