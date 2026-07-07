"""Model registry loading and validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Any


class RegistryError(ValueError):
    """Raised when registry data is invalid or unsupported."""


@dataclass(frozen=True)
class ModelRegistry:
    data: Mapping[str, Any]
    sha256: str

    @property
    def default_engine(self) -> str:
        return _require_non_empty_string(self.data.get("default_engine"), "default_engine")

    @property
    def default_language(self) -> str:
        return _require_non_empty_string(self.data.get("default_language"), "default_language")

    def engine_ids(self) -> set[str]:
        return {_require_non_empty_string(engine.get("id"), "engine id") for engine in self.data["engines"]}

    def model_ids(self, engine_id: str) -> set[str]:
        engine = self.engine(engine_id)
        return {_require_non_empty_string(model.get("id"), f"{engine_id} model id") for model in engine["models"]}

    def default_model(self, engine_id: str) -> str:
        return _require_non_empty_string(
            self.engine(engine_id).get("default_model"),
            f"{engine_id} default_model",
        )

    def engine(self, engine_id: str) -> Mapping[str, Any]:
        for engine in self.data["engines"]:
            if engine["id"] == engine_id:
                return engine
        raise RegistryError(f"Unknown engine: {engine_id}")

    def validate_engine_model(self, engine_id: str, model_id: str | None) -> str:
        if engine_id not in self.engine_ids():
            raise RegistryError(f"Unknown engine: {engine_id}")
        if model_id is None:
            return self.default_model(engine_id)
        if model_id not in self.model_ids(engine_id):
            raise RegistryError(f"Unknown model for {engine_id}: {model_id}")
        return model_id

    def language_for_engine(self, language: str, engine_id: str) -> str:
        languages = self.data.get("languages", {})
        lang_entry = languages.get(language)
        if not isinstance(lang_entry, Mapping):
            raise RegistryError(f"Unknown language: {language}")
        engine_value = lang_entry.get("engines", {}).get(engine_id)
        if not isinstance(engine_value, str):
            raise RegistryError(f"Language {language} is unsupported for {engine_id}")
        return engine_value


def _registry_bytes() -> bytes:
    resource = files("zen_whisper_mac_backend").joinpath(
        "resources/model_registry.json"
    )
    return resource.read_bytes()


def load_registry() -> ModelRegistry:
    raw = _registry_bytes()
    data = json.loads(raw.decode("utf-8"))
    registry = ModelRegistry(data=_freeze(data), sha256=hashlib.sha256(raw).hexdigest())
    _validate_registry(registry)
    return registry


def _validate_registry(registry: ModelRegistry) -> None:
    if registry.data.get("version") != 1:
        raise RegistryError("unsupported registry version")
    _require_unique_ids(registry.data["engines"], "engine")
    if registry.default_engine not in registry.engine_ids():
        raise RegistryError("default_engine is not listed in engines")
    if registry.default_language not in registry.data.get("languages", {}):
        raise RegistryError("default_language is not listed in languages")
    for engine in registry.data["engines"]:
        engine_id = _require_non_empty_string(engine.get("id"), "engine id")
        _require_non_empty_string(engine.get("label"), f"{engine_id} label")
        _require_unique_ids(engine["models"], f"{engine_id} model")
        default_model = _require_non_empty_string(
            engine.get("default_model"),
            f"{engine_id} default_model",
        )
        if default_model not in registry.model_ids(engine_id):
            raise RegistryError(f"default_model is not listed for {engine_id}")
    engine_ids = registry.engine_ids()
    languages = registry.data.get("languages", {})
    if not isinstance(languages, Mapping):
        raise RegistryError("languages must be an object")
    for language_id, language in languages.items():
        if not isinstance(language, Mapping):
            raise RegistryError(f"language entry is not an object: {language_id}")
        _require_non_empty_string(language.get("label"), f"language {language_id} label")
        engine_map = language.get("engines")
        if not isinstance(engine_map, Mapping):
            raise RegistryError(f"language entry missing engines: {language_id}")
        for engine_id, engine_language in engine_map.items():
            if engine_id not in engine_ids:
                raise RegistryError(f"language {language_id} references unknown engine: {engine_id}")
            if not isinstance(engine_language, str) or not engine_language:
                raise RegistryError(f"language {language_id} has invalid engine value for {engine_id}")


def _require_unique_ids(items: object, label: str) -> None:
    if not isinstance(items, tuple):
        raise RegistryError(f"{label} list is not an array")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise RegistryError(f"{label} entry is not an object")
        item_id = _require_non_empty_string(item.get("id"), f"{label} id")
        _require_non_empty_string(item.get("label"), f"{label} label")
        if item_id in seen:
            raise RegistryError(f"duplicate {label} id: {item_id}")
        seen.add(item_id)


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{label} must be a non-empty string")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
