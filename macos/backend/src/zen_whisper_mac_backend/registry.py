"""Model registry loading and validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


class RegistryError(ValueError):
    """Raised when registry data is invalid or unsupported."""


@dataclass(frozen=True)
class ModelRegistry:
    data: dict[str, Any]
    sha256: str

    @property
    def default_engine(self) -> str:
        return str(self.data["default_engine"])

    @property
    def default_language(self) -> str:
        return str(self.data["default_language"])

    def engine_ids(self) -> set[str]:
        return {str(engine["id"]) for engine in self.data["engines"]}

    def model_ids(self, engine_id: str) -> set[str]:
        engine = self.engine(engine_id)
        return {str(model["id"]) for model in engine["models"]}

    def default_model(self, engine_id: str) -> str:
        return str(self.engine(engine_id)["default_model"])

    def engine(self, engine_id: str) -> dict[str, Any]:
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
        if not isinstance(lang_entry, dict):
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
    registry = ModelRegistry(data=data, sha256=hashlib.sha256(raw).hexdigest())
    _validate_registry(registry)
    return registry


def _validate_registry(registry: ModelRegistry) -> None:
    if registry.default_engine not in registry.engine_ids():
        raise RegistryError("default_engine is not listed in engines")
    if registry.default_language not in registry.data.get("languages", {}):
        raise RegistryError("default_language is not listed in languages")
    for engine in registry.data["engines"]:
        engine_id = str(engine["id"])
        default_model = str(engine["default_model"])
        if default_model not in registry.model_ids(engine_id):
            raise RegistryError(f"default_model is not listed for {engine_id}")
