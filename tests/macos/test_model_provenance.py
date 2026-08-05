"""macOS registry model downloads stay pinned and content-verified."""

from __future__ import annotations

import hashlib
import sys
import types
from fnmatch import fnmatchcase
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
sys.path.insert(0, str(BACKEND_SRC))

import zen_whisper_mac_backend.adapters as adapters_module  # noqa: E402
from zen_whisper_mac_backend.adapters import _pinned_model_snapshot  # noqa: E402
from zen_whisper_mac_backend.adapters import (  # noqa: E402
    MlxQwen3AsrAdapter,
    MlxWhisperAdapter,
)
from zen_whisper_mac_backend.registry import (  # noqa: E402
    ModelRegistry,
    RegistryError,
    _freeze,
    _validate_registry,
    load_registry,
)


def test_bundled_registry_models_declare_full_revision_and_hashes() -> None:
    registry = load_registry()
    for engine_id in ("mlx-whisper", "mlx-qwen3-asr"):
        for model_id in registry.model_ids(engine_id):
            model = registry.model(engine_id, model_id)
            assert len(model["revision"]) == 40
            assert model["allow_patterns"]
            assert model["files"]
            assert all(len(value["sha256"]) == 64 for value in model["files"].values())
            assert not any(
                fnmatchcase("unreviewed_modeling.py", pattern)
                for pattern in model["allow_patterns"]
            )


def test_registry_rejects_non_object_root_and_boolean_version() -> None:
    with pytest.raises(RegistryError, match="root"):
        _validate_registry(ModelRegistry(_freeze([]), "hash"))  # type: ignore[arg-type]
    with pytest.raises(RegistryError, match="version"):
        _validate_registry(ModelRegistry(_freeze({"version": True}), "hash"))


@pytest.mark.parametrize(
    ("provenance", "message"),
    [
        ({"revision": "a" * 40}, "allow_patterns"),
        (
            {
                "revision": "main",
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "0" * 64, "size": 1}},
            },
            "full commit",
        ),
        (
            {
                "revision": "a" * 40,
                "allow_patterns": ["*"],
                "files": {"../weights.bin": {"sha256": "0" * 64, "size": 1}},
            },
            "filename",
        ),
        (
            {
                "revision": "a" * 40,
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "x" * 64, "size": 1}},
            },
            "hash",
        ),
        (
            {
                "revision": "a" * 40,
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "0" * 64, "size": 0}},
            },
            "size",
        ),
    ],
)
def test_registry_rejects_partial_or_unsafe_model_provenance(
    provenance: dict[str, object],
    message: str,
) -> None:
    model = {"id": "owner/model", "label": "Model", **provenance}
    data = {
        "version": 1,
        "default_engine": "mlx-whisper",
        "default_language": "ja",
        "languages": {
            "ja": {"label": "Japanese", "engines": {"mlx-whisper": "ja"}}
        },
        "engines": [
            {
                "id": "mlx-whisper",
                "label": "MLX Whisper",
                "default_model": "owner/model",
                "models": [model],
            }
        ],
    }

    with pytest.raises(RegistryError, match=message):
        _validate_registry(ModelRegistry(_freeze(data), "hash"))


def test_pinned_model_snapshot_passes_commit_and_verifies_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"a" * (1024 * 1024 + 1)
    revision = "e" * 40
    snapshot = tmp_path / revision
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(payload)
    entry = {
        "id": "owner/model",
        "revision": revision,
        "allow_patterns": ("weights.bin",),
        "files": {
            "weights.bin": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        },
    }
    monkeypatch.setattr(
        adapters_module,
        "load_registry",
        lambda: types.SimpleNamespace(model=lambda _engine, _model: entry),
    )
    calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return str(snapshot)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    assert _pinned_model_snapshot("mlx-whisper", "owner/model") == str(snapshot)
    assert calls == [
        {
            "repo_id": "owner/model",
            "revision": revision,
            "allow_patterns": ["weights.bin"],
        }
    ]

    (snapshot / "weights.bin").write_bytes(payload[:-1] + b"b")
    with pytest.raises(RegistryError, match="hash mismatch"):
        _pinned_model_snapshot("mlx-whisper", "owner/model")


def test_pinned_model_snapshot_rejects_wrong_commit_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = "e" * 40
    snapshot = tmp_path / ("f" * 40)
    snapshot.mkdir()
    payload = b"model"
    (snapshot / "weights.bin").write_bytes(payload)
    entry = {
        "id": "owner/model",
        "revision": revision,
        "allow_patterns": ("weights.bin",),
        "files": {
            "weights.bin": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        },
    }
    monkeypatch.setattr(
        adapters_module,
        "load_registry",
        lambda: types.SimpleNamespace(model=lambda _engine, _model: entry),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=lambda **_kwargs: str(snapshot)),
    )

    with pytest.raises(RegistryError, match="commit"):
        _pinned_model_snapshot("mlx-whisper", "owner/model")


def test_mlx_whisper_loader_receives_verified_local_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    verified = "/verified/mlx-whisper-snapshot"
    monkeypatch.setattr(
        adapters_module,
        "_pinned_model_snapshot",
        lambda _engine, _model: verified,
    )

    def fake_transcribe(_audio: object, **kwargs: object) -> dict[str, str]:
        calls.append(str(kwargs["path_or_hf_repo"]))
        return {"text": ""}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=fake_transcribe),
    )

    MlxWhisperAdapter().preload("mlx-community/whisper-small-mlx", "ja")

    assert calls == [verified]


def test_mlx_qwen_loader_receives_verified_local_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    verified = "/verified/mlx-qwen-snapshot"
    monkeypatch.setattr(
        adapters_module,
        "_pinned_model_snapshot",
        lambda _engine, _model: verified,
    )
    mlx_audio = types.ModuleType("mlx_audio")
    mlx_audio.__path__ = []  # type: ignore[attr-defined]
    stt = types.ModuleType("mlx_audio.stt")

    def fake_load(path: str) -> object:
        calls.append(path)
        return types.SimpleNamespace(generate=lambda *_args, **_kwargs: None)

    stt.load = fake_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", mlx_audio)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt)

    MlxQwen3AsrAdapter().preload("mlx-community/Qwen3-ASR-0.6B-8bit", "Japanese")

    assert calls == [verified]


def test_verification_failure_prevents_mlx_loader_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def reject_snapshot(_engine: str, _model: str) -> str:
        raise RegistryError("model hash mismatch")

    monkeypatch.setattr(adapters_module, "_pinned_model_snapshot", reject_snapshot)
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        types.SimpleNamespace(transcribe=lambda *args, **kwargs: calls.append((args, kwargs))),
    )

    with pytest.raises(Exception, match="MLX Whisper unavailable"):
        MlxWhisperAdapter().preload("mlx-community/whisper-small-mlx", "ja")

    assert calls == []
