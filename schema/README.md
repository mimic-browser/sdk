# Wire contract sources

`mimic/protocol.json` is the current SDK-owned Mimic extension schema. It was
audited against the handler revision recorded in `mimic/source.json`. Source
hashes establish provenance; they do not select parallel runtime API versions.
The runtime performs stateful coherence checks that JSON Schema alone cannot
express, including native media availability, profile recipe coherence,
resource-rule relationships and Context ownership.

The current contract exposes 32 command entry points. There are no
Mimic-domain notifications. Trace records are results of `Mimic.getTrace`, not
invented push events. Ordinary CDP events stay owned by the automation client.

Portable integer counters are limited to the exact IEEE-754 integer range in
the stable cross-language contract. The raw transport remains the explicit path
for new or nonportable wire payloads. Snapshot files contain base64 strings;
they are not filesystem paths or implicitly decoded output files.

`sources/chrome152/` contains exact retained bytes and original capture metadata
from the runtime repository. This is existing evidence, not a new browser run.
The original metadata's sourcePath describes the runtime capture location and
is deliberately retained unchanged. The generator verifies the source hash
offline and does not launch Chrome, Mimic or a network listener.

Generate with `python generator/generate.py` and verify with
`python generator/generate.py --check`. Run the shared schema and Python model
corpus with `python -m unittest discover -s generator -p test_contract.py`.
Language-native conformance tests consume the same
`conformance/fixtures/wire.json` cases through their actual serializers.
