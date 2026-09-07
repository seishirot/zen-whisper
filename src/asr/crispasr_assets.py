"""Offline allowlist and installation checks for the optional Windows runtime."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[1] / "crispasr_manifest.json").read_text(encoding="utf-8"))


def installation_root(configured: str) -> Path:
    path = Path(configured) if configured else ROOT / "tools/bin/crispasr/v0.8.32"
    return (path if path.is_absolute() else ROOT / path).resolve()


def profile_label(profile: str) -> str:
    return manifest()["models"].get(profile, {}).get("label", profile)


def profile_spec(profile: str) -> dict:
    try:
        return manifest()["models"][profile]
    except (KeyError, TypeError):
        raise ValueError("CrispASR: allowlistのモデルを選択してください") from None


def decoder_for(profile: dict, requested: str) -> str:
    decoder = profile["decoders"][0] if requested == "auto" else requested
    if decoder not in profile["decoders"]:
        raise ValueError("CrispASR: このモデルに対応するdecoderを選択してください（auto推奨）")
    return decoder


def verify_hash(path: Path, expected: dict) -> None:
    if not path.is_file() or path.stat().st_size != expected["size"]:
        raise ValueError(f"CrispASR: 未配置またはサイズ不一致: {path.name}")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != expected["sha256"]:
        raise ValueError(f"CrispASR: SHA-256不一致: {path.name}")


def resolve_installation(root: Path, device: str, model: str, *, verify: bool) -> tuple[Path, Path]:
    data = manifest()
    if device not in data["runtimes"]:
        raise ValueError("CrispASRはcpuまたはcudaを明示選択してください")
    profile = profile_spec(model)
    runtime = data["runtimes"][device]
    directory = root / device
    executable = directory / runtime["asset"].removesuffix(".zip") / "crispasr.exe"
    model_path = root / "models" / profile["file"]
    receipt_path = directory / "receipt.json"
    if not executable.is_file() or not receipt_path.is_file() or not model_path.is_file():
        raise ValueError("CrispASRが未配置です。tools/setup_crispasr.pyでruntimeとmodelを明示導入してください")
    if verify:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("archive_sha256") != runtime["sha256"] or receipt.get("commit") != data["commit"]:
            raise ValueError("CrispASR: setup receiptが固定releaseと一致しません")
        files = receipt["files"]
        if executable.relative_to(directory).as_posix() not in files:
            raise ValueError("CrispASR: executableの検証記録がありません")
        for name, spec in files.items():
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()):
                raise ValueError("CrispASR: 不正なreceipt path")
            verify_hash(path, spec)
        verify_hash(model_path, profile)
    return executable, model_path
