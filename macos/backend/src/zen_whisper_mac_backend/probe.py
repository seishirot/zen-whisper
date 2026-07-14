"""Runtime and packaging probes shared by macOS installers and app validation."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import Any


def backend_code_hash_from_package(package_dir: Path | None = None) -> str:
    import zen_whisper_mac_backend

    package_dir = package_dir or Path(zen_whisper_mac_backend.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        if not _should_hash_package_file(path):
            continue
        rel = "zen_whisper_mac_backend/" + path.relative_to(package_dir).as_posix()
        _update_digest(digest, rel, path.read_bytes())
    return digest.hexdigest()


def backend_code_hash_from_wheel(wheel_path: Path) -> str:
    digest = hashlib.sha256()
    with zipfile.ZipFile(wheel_path) as wheel:
        for name in sorted(wheel.namelist()):
            if not _should_hash_wheel_name(name):
                continue
            _update_digest(digest, name, wheel.read(name))
    return digest.hexdigest()


def runtime_probe() -> dict[str, Any]:
    import zen_whisper_mac_backend
    from zen_whisper_mac_backend.registry import load_registry

    return {
        "protocol_version": zen_whisper_mac_backend.PROTOCOL_VERSION,
        "backend_version": zen_whisper_mac_backend.BACKEND_VERSION,
        "python_arch": platform.machine(),
        "registry_hash": load_registry().sha256,
        "backend_code_hash": backend_code_hash_from_package(),
    }


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "wheel-hash":
        print(backend_code_hash_from_wheel(Path(sys.argv[2])))
        return
    if len(sys.argv) == 2 and sys.argv[1] == "runtime-probe":
        print(json.dumps(runtime_probe(), sort_keys=True))
        return
    raise SystemExit("usage: probe.py wheel-hash WHEEL | runtime-probe")


def _should_hash_package_file(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )


def _should_hash_wheel_name(name: str) -> bool:
    return name.startswith("zen_whisper_mac_backend/") and not name.endswith("/")


def _update_digest(digest: "hashlib._Hash", name: str, content: bytes) -> None:
    file_hash = hashlib.sha256(content).hexdigest()
    digest.update(f"{name}\0{file_hash}\n".encode())


if __name__ == "__main__":
    main()
