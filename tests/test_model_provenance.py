"""Immutable model-source resolution and integrity checks."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

import src.model_provenance as model_provenance
from src.model_provenance import (
    MODEL_SOURCES,
    ExpectedFile,
    ModelProvenanceError,
    ModelSource,
    download_verified_snapshot,
    resolve_model,
    _verify_file,
)


def test_reviewed_manifest_has_full_commits_and_hashed_allowed_files() -> None:
    assert MODEL_SOURCES
    for models in MODEL_SOURCES.values():
        assert models
        for source in models.values():
            assert len(source.revision) == 40
            assert source.allow_patterns
            assert source.files
            assert all(len(expected.sha256) == 64 for expected in source.files.values())
            assert not any(
                fnmatchcase("unreviewed_modeling.py", pattern)
                for pattern in source.allow_patterns
            )


def test_reviewed_manifest_cannot_be_mutated_after_validation() -> None:
    group_name = next(iter(MODEL_SOURCES))
    model_name = next(iter(MODEL_SOURCES[group_name]))
    source = MODEL_SOURCES[group_name][model_name]
    filename = next(iter(source.files))

    with pytest.raises(TypeError):
        MODEL_SOURCES[group_name] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        MODEL_SOURCES[group_name][model_name] = source  # type: ignore[index]
    with pytest.raises(TypeError):
        source.files[filename] = source.files[filename]  # type: ignore[index]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"version": True, "groups": {}},
    ],
)
def test_manifest_rejects_non_object_root_and_boolean_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: object,
) -> None:
    manifest = tmp_path / "model_manifest.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(model_provenance, "_MANIFEST_PATH", manifest)

    with pytest.raises(ModelProvenanceError, match="unsupported"):
        model_provenance._load_manifest()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {
                "repo_id": "owner/model",
                "revision": "main",
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "0" * 64, "size": 1}},
                "license": "MIT",
            },
            "full commit",
        ),
        (
            {
                "repo_id": "owner/model",
                "revision": "a" * 40,
                "allow_patterns": ["config.json"],
                "files": {"weights.bin": {"sha256": "0" * 64, "size": 1}},
                "license": "MIT",
            },
            "filename",
        ),
        (
            {
                "repo_id": "owner/model",
                "revision": "a" * 40,
                "allow_patterns": ["*"],
                "files": {"../weights.bin": {"sha256": "0" * 64, "size": 1}},
                "license": "MIT",
            },
            "filename",
        ),
        (
            {
                "repo_id": "owner/model",
                "revision": "a" * 40,
                "allow_patterns": ["weights.bin"],
                "files": {},
                "license": "MIT",
            },
            "empty",
        ),
        (
            {
                "repo_id": "owner/model",
                "revision": "a" * 40,
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "x" * 64, "size": 1}},
                "license": "MIT",
            },
            "hash",
        ),
        (
            {
                "repo_id": "owner/model",
                "revision": "a" * 40,
                "allow_patterns": ["weights.bin"],
                "files": {"weights.bin": {"sha256": "0" * 64, "size": 0}},
                "license": "MIT",
            },
            "size",
        ),
    ],
)
def test_manifest_parser_rejects_partial_or_unsafe_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry: dict[str, object],
    message: str,
) -> None:
    manifest = tmp_path / "model_manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "groups": {"test": {"model": entry}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_provenance, "_MANIFEST_PATH", manifest)

    with pytest.raises(ModelProvenanceError, match=message):
        model_provenance._load_manifest()


def test_resolve_model_accepts_reviewed_local_and_explicit_commit(
    tmp_path: Path,
) -> None:
    reviewed = resolve_model("faster_whisper", "turbo", aliases={"turbo": "large-v3-turbo"})
    assert reviewed.model_source is MODEL_SOURCES["faster_whisper"]["large-v3-turbo"]

    local = resolve_model("faster_whisper", str(tmp_path))
    assert local.local is True
    assert Path(local.source) == tmp_path.resolve()

    revision = "a" * 40
    pinned = resolve_model(
        "faster_whisper",
        f"owner/model@{revision}",
        custom_allow_patterns=("*.json", "*.bin"),
    )
    assert pinned.source == "owner/model"
    assert pinned.revision == revision
    assert pinned.model_source is not None
    assert pinned.model_source.files == {}


def test_built_in_model_cannot_be_shadowed_by_a_relative_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "tiny").mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = resolve_model("faster_whisper", "tiny")
    explicit_local = resolve_model("faster_whisper", str(tmp_path / "tiny"))

    assert resolved.local is False
    assert resolved.model_source is MODEL_SOURCES["faster_whisper"]["tiny"]
    assert explicit_local.local is True


@pytest.mark.parametrize("configured", ["", " ", "\t"])
def test_resolve_model_rejects_empty_source(configured: str) -> None:
    with pytest.raises(ModelProvenanceError, match="空"):
        resolve_model("faster_whisper", configured)


@pytest.mark.parametrize("configured", ["owner/model", "model-name", "owner/model@main"])
def test_resolve_model_rejects_mutable_or_unknown_remote(configured: str) -> None:
    with pytest.raises(ModelProvenanceError, match="40桁commit"):
        resolve_model("faster_whisper", configured)


def test_download_uses_full_revision_and_verifies_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"reviewed model bytes"
    revision = "b" * 40
    snapshot = tmp_path / revision
    snapshot.mkdir()
    (snapshot / "weights.bin").write_bytes(payload)
    calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return str(snapshot)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=fake_snapshot_download),
    )
    source = ModelSource(
        repo_id="owner/model",
        revision=revision,
        allow_patterns=("config.json", "weights.bin"),
        files={
            "weights.bin": ExpectedFile(
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        },
        license="Apache-2.0",
    )

    assert download_verified_snapshot(source) == str(snapshot)
    assert calls == [
        {
            "repo_id": "owner/model",
            "revision": revision,
            "allow_patterns": ["config.json", "weights.bin"],
            "local_files_only": False,
        }
    ]

    (snapshot / "weights.bin").write_bytes(b"tampered model bytes")
    with pytest.raises(ModelProvenanceError, match="size mismatch|hash mismatch"):
        download_verified_snapshot(source)


def test_download_rejects_wrong_snapshot_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wrong_snapshot = tmp_path / ("c" * 40)
    wrong_snapshot.mkdir()
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=lambda **_kwargs: str(wrong_snapshot)),
    )
    source = ModelSource(
        repo_id="owner/model",
        revision="d" * 40,
        allow_patterns=("weights.bin",),
        files={"weights.bin": ExpectedFile(sha256="0" * 64, size=1)},
        license="MIT",
    )

    with pytest.raises(ModelProvenanceError, match="approved commit"):
        download_verified_snapshot(source)


def test_content_addressed_filename_does_not_bypass_hash_verification(
    tmp_path: Path,
) -> None:
    expected_payload = b"expected"
    expected = ExpectedFile(
        sha256=hashlib.sha256(expected_payload).hexdigest(),
        size=len(expected_payload),
    )
    cache_blob = tmp_path / expected.sha256
    cache_blob.write_bytes(b"tampered")

    with pytest.raises(ModelProvenanceError, match="hash mismatch"):
        _verify_file(cache_blob, expected)
