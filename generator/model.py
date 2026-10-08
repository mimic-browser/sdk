"""Offline canonical schema loader and small, fail-closed JSON Schema evaluator.

The evaluator covers the vocabulary used by the current contract. It is not a
general-purpose replacement for JSON Schema. Unknown vocabulary fails generation
so an extension cannot accidentally lose validation in one target.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema/mimic/protocol.json"
VOCABULARY = {
    "$ref", "type", "properties", "required", "additionalProperties", "items",
    "oneOf", "anyOf", "not", "enum", "const", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "format",
    "contentEncoding", "description", "title", "default",
}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Contract:
    source: dict
    sha256: str

    @property
    def definitions(self) -> dict:
        return self.source["$defs"]

    @property
    def commands(self) -> list[dict]:
        return self.source["commands"]

    def resolve(self, schema: dict) -> dict:
        if "$ref" in schema:
            return self.definitions[schema["$ref"].removeprefix("#/$defs/")]
        return schema

    def command(self, method: str) -> dict:
        for command in self.commands:
            if command["name"] == method:
                return command
        raise ContractError(f"Unknown stable command: {method}; use explicit raw dispatch")

    def validate(self, schema: dict, value: Any, path: str = "$") -> None:
        if "$ref" in schema:
            self.validate(self.resolve(schema), value, path)
            return
        if "oneOf" in schema or "anyOf" in schema:
            key = "oneOf" if "oneOf" in schema else "anyOf"
            matches = 0
            for choice in schema[key]:
                try:
                    self.validate(choice, value, path)
                    matches += 1
                except ContractError:
                    pass
            if (key == "oneOf" and matches != 1) or not matches:
                raise ContractError(f"{path}: {key} did not match")
        if "not" in schema:
            try:
                self.validate(schema["not"], value, path)
            except ContractError:
                pass
            else:
                raise ContractError(f"{path}: forbidden combination")
        kind = schema.get("type")
        valid = {
            "object": lambda: isinstance(value, dict),
            "array": lambda: isinstance(value, list),
            "string": lambda: isinstance(value, str),
            "boolean": lambda: isinstance(value, bool),
            "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
            "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
            "null": lambda: value is None,
        }
        if kind is not None and not valid[kind]():
            raise ContractError(f"{path}: expected {kind}")
        if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
            raise ContractError(f"{path}: unexpected constant")
        if "enum" in schema and value not in schema["enum"]:
            raise ContractError(f"{path}: unknown enum value")
        if isinstance(value, dict):
            for required in schema.get("required", []):
                if required not in value:
                    raise ContractError(f"{path}.{required}: required")
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for key, child in value.items():
                if key in properties:
                    self.validate(properties[key], child, path + "." + key)
                elif additional is False:
                    raise ContractError(f"{path}.{key}: unknown field")
                elif isinstance(additional, dict):
                    self.validate(additional, child, path + "." + key)
        if isinstance(value, list):
            for index, item in enumerate(value):
                self.validate(schema.get("items", {}), item, f"{path}[{index}]")
            if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
                raise ContractError(f"{path}: duplicate item")
        for key, measure, compare in (
            ("minimum", value, lambda x, y: x < y),
            ("maximum", value, lambda x, y: x > y),
            ("minLength", len(value) if isinstance(value, str) else 0, lambda x, y: x < y),
            ("maxLength", len(value) if isinstance(value, str) else 0, lambda x, y: x > y),
            ("minItems", len(value) if isinstance(value, list) else 0, lambda x, y: x < y),
            ("maxItems", len(value) if isinstance(value, list) else 0, lambda x, y: x > y),
        ):
            if key in schema and compare(measure, schema[key]):
                raise ContractError(f"{path}: violates {key}")


def load(path: Path = SCHEMA) -> Contract:
    raw = path.read_bytes()
    source = json.loads(raw)
    contract = Contract(source, hashlib.sha256(raw).hexdigest())
    if source.get("events") != []:
        raise ContractError("New Mimic events require an explicit generation/backend implementation")
    names = set()
    for command in contract.commands:
        if not re.fullmatch(r"Mimic\.[a-z][A-Za-z0-9]*", command["name"]) or command["name"] in names:
            raise ContractError("Invalid/duplicate command name")
        names.add(command["name"])
        if command["scope"] not in ("browser", "context", "session"):
            raise ContractError("Unknown command scope")

    def inspect(schema: dict) -> None:
        for key in schema:
            if key not in VOCABULARY:
                raise ContractError(f"Unsupported schema vocabulary: {key}")
        if "$ref" in schema:
            ref = schema["$ref"]
            if not ref.startswith("#/$defs/") or ref.removeprefix("#/$defs/") not in contract.definitions:
                raise ContractError(f"Unresolved reference: {ref}")
        properties = schema.get("properties", {})
        if "properties" in schema and not set(schema.get("required", [])).issubset(properties):
            raise ContractError("Required property is undeclared")
        for child in properties.values():
            inspect(child)
        for key in ("items", "not", "additionalProperties"):
            if isinstance(schema.get(key), dict):
                inspect(schema[key])
        for key in ("oneOf", "anyOf"):
            for child in schema.get(key, []):
                inspect(child)

    for schema in contract.definitions.values():
        inspect(schema)
    for command in contract.commands:
        inspect(command["params"])
        inspect(command["result"])
    return contract
