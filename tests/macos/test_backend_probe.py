from __future__ import annotations

import sys
import zipfile
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
sys.path.insert(0, str(BACKEND_SRC))

from zen_whisper_mac_backend.probe import (  # noqa: E402
    backend_code_hash_from_package,
    backend_code_hash_from_wheel,
)


def test_backend_probe_hashes_package_and_wheel_consistently(tmp_path: Path) -> None:
    package_dir = tmp_path / "zen_whisper_mac_backend"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("BACKEND_VERSION='x'\n", encoding="utf-8")
    (package_dir / "probe.py").write_text("x=1\n", encoding="utf-8")
    (package_dir / "__pycache__").mkdir()
    (package_dir / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
    wheel = tmp_path / "backend.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(package_dir / "__init__.py", "zen_whisper_mac_backend/__init__.py")
        archive.write(package_dir / "probe.py", "zen_whisper_mac_backend/probe.py")

    assert backend_code_hash_from_package(package_dir) == backend_code_hash_from_wheel(wheel)


def test_probe_wheel_hash_cli_does_not_require_importable_backend_package(tmp_path: Path) -> None:
    wheel = tmp_path / "backend.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("zen_whisper_mac_backend/__init__.py", "")

    result = subprocess.run(
        [
            sys.executable,
            "-P",
            str(BACKEND_SRC / "zen_whisper_mac_backend/probe.py"),
            "wheel-hash",
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )

    assert len(result.stdout.strip()) == 64
