"""Model-source resolution and Hugging Face snapshot integrity checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_ID_PATTERN = (
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
_REPO_ID_RE = re.compile(rf"^{_REPO_ID_PATTERN}$")
_PINNED_REMOTE_RE = re.compile(
    rf"^(?P<repo>{_REPO_ID_PATTERN})"
    r"@(?P<revision>[0-9a-f]{40})$"
)
_MANIFEST_PATH = Path(__file__).with_name("model_manifest.json")


class ModelProvenanceError(ValueError):
    """Raised when a model source is mutable or fails integrity verification."""


@dataclass(frozen=True)
class ExpectedFile:
    sha256: str
    size: int


@dataclass(frozen=True)
class ModelSource:
    repo_id: str
    revision: str
    allow_patterns: tuple[str, ...]
    files: Mapping[str, ExpectedFile]
    license: str


@dataclass(frozen=True)
class ResolvedModel:
    source: str
    revision: str | None
    local: bool
    model_source: ModelSource | None


def _load_manifest() -> Mapping[str, Mapping[str, ModelSource]]:
    raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw.get("version") != 1
        or not isinstance(raw.get("groups"), dict)
    ):
        raise ModelProvenanceError("unsupported model manifest")

    groups: dict[str, dict[str, ModelSource]] = {}
    for group_name, entries in raw["groups"].items():
        if not isinstance(entries, dict):
            raise ModelProvenanceError(f"invalid model group: {group_name}")
        parsed: dict[str, ModelSource] = {}
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                raise ModelProvenanceError(f"invalid model entry: {key}")
            repo_id = entry.get("repo_id")
            if not isinstance(repo_id, str) or not _REPO_ID_RE.fullmatch(repo_id):
                raise ModelProvenanceError(f"invalid model repository: {key}")
            revision = entry.get("revision")
            if not isinstance(revision, str) or not _FULL_COMMIT_RE.fullmatch(revision):
                raise ModelProvenanceError(f"model revision is not a full commit: {key}")
            allow_patterns = entry.get("allow_patterns")
            if (
                not isinstance(allow_patterns, list)
                or not allow_patterns
                or any(not isinstance(pattern, str) or not pattern for pattern in allow_patterns)
            ):
                raise ModelProvenanceError(f"invalid model allow list: {key}")
            raw_files = entry.get("files")
            if not isinstance(raw_files, dict) or not raw_files:
                raise ModelProvenanceError(f"model file manifest is empty: {key}")
            files: dict[str, ExpectedFile] = {}
            for filename, expected in raw_files.items():
                if (
                    not isinstance(filename, str)
                    or not filename
                    or "\\" in filename
                    or Path(filename).is_absolute()
                    or ".." in Path(filename).parts
                    or not any(fnmatchcase(filename, pattern) for pattern in allow_patterns)
                ):
                    raise ModelProvenanceError(f"invalid model filename: {key}/{filename}")
                if not isinstance(expected, dict):
                    raise ModelProvenanceError(f"invalid model file metadata: {key}/{filename}")
                sha256 = expected.get("sha256")
                size = expected.get("size")
                if not isinstance(sha256, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", sha256
                ):
                    raise ModelProvenanceError(f"invalid model file hash: {key}/{filename}")
                if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                    raise ModelProvenanceError(f"invalid model file size: {key}/{filename}")
                files[filename] = ExpectedFile(sha256=sha256, size=size)
            license_id = entry.get("license")
            if not isinstance(license_id, str) or not license_id:
                raise ModelProvenanceError(f"invalid model license: {key}")
            parsed[key] = ModelSource(
                repo_id=repo_id,
                revision=revision,
                allow_patterns=tuple(allow_patterns),
                files=MappingProxyType(files),
                license=license_id,
            )
        groups[group_name] = parsed
    return MappingProxyType(
        {group_name: MappingProxyType(entries) for group_name, entries in groups.items()}
    )


MODEL_SOURCES = _load_manifest()


def model_source(group: str, key: str) -> ModelSource:
    """Return a reviewed built-in model source."""
    try:
        return MODEL_SOURCES[group][key]
    except KeyError as exc:
        raise ModelProvenanceError(f"unapproved model source: {group}/{key}") from exc


def resolve_model(
    group: str,
    configured: str,
    *,
    aliases: Mapping[str, str] | None = None,
    custom_allow_patterns: tuple[str, ...] = (),
) -> ResolvedModel:
    """Resolve a local path, reviewed built-in, or explicit ``repo@commit``."""
    candidate = configured.strip()
    if not candidate:
        raise ModelProvenanceError("モデル指定は空にできません")

    key = aliases.get(candidate, candidate) if aliases is not None else candidate
    source = MODEL_SOURCES.get(group, {}).get(key)
    if source is not None:
        return ResolvedModel(
            source=source.repo_id,
            revision=source.revision,
            local=False,
            model_source=source,
        )

    match = _PINNED_REMOTE_RE.fullmatch(candidate)
    if match is not None:
        explicit = ModelSource(
            repo_id=match.group("repo"),
            revision=match.group("revision"),
            allow_patterns=custom_allow_patterns,
            files=MappingProxyType({}),
            license="user-supplied",
        )
        return ResolvedModel(
            source=explicit.repo_id,
            revision=explicit.revision,
            local=False,
            model_source=explicit,
        )

    local_path = Path(candidate).expanduser()
    if local_path.is_dir():
        return ResolvedModel(
            source=str(local_path.resolve()),
            revision=None,
            local=True,
            model_source=None,
        )

    raise ModelProvenanceError(
        "リモートモデルは組み込みID、ローカルディレクトリ、または "
        "repo/model@40桁commit の形式で指定してください"
    )


def download_verified_snapshot(
    source: ModelSource,
    *,
    required_files: tuple[str, ...] | None = None,
    local_files_only: bool = False,
) -> str:
    """Download a commit-pinned snapshot and verify any declared artifacts."""
    from huggingface_hub import snapshot_download

    allow_patterns = source.allow_patterns
    expected_files = source.files
    if required_files is not None:
        missing_metadata = sorted(set(required_files) - set(source.files))
        if missing_metadata:
            raise ModelProvenanceError(
                f"model manifest lacks required files: {missing_metadata[0]}"
            )
        allow_patterns = required_files
        expected_files = {name: source.files[name] for name in required_files}

    snapshot = Path(
        snapshot_download(
            repo_id=source.repo_id,
            revision=source.revision,
            allow_patterns=list(allow_patterns),
            local_files_only=local_files_only,
        )
    )
    if snapshot.name.lower() != source.revision:
        raise ModelProvenanceError(
            f"resolved snapshot does not match approved commit: {source.repo_id}"
        )
    for filename, expected in expected_files.items():
        _verify_file(snapshot / filename, expected)
    return str(snapshot)


def _verify_file(path: Path, expected: ExpectedFile) -> None:
    if not path.is_file():
        raise ModelProvenanceError(f"approved model file is missing: {path.name}")
    actual_size = path.stat().st_size
    if actual_size != expected.size:
        raise ModelProvenanceError(f"model file size mismatch: {path.name}")

    # Do not trust the content-addressed cache filename alone: a damaged or
    # locally replaced blob can retain the expected name and size.
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.sha256:
        raise ModelProvenanceError(f"model file hash mismatch: {path.name}")
