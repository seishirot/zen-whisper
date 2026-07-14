from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"
INSTALL_BACKEND = REPO_ROOT / "macos/scripts/install_backend_from_app.sh"


def test_backend_launch_and_probe_use_safe_python_path() -> None:
    backend_client = (SWIFT_SRC / "BackendClient.swift").read_text(encoding="utf-8")
    validator = (SWIFT_SRC / "BackendInstallValidator.swift").read_text(encoding="utf-8")
    environment = (SWIFT_SRC / "ProcessEnvironment.swift").read_text(encoding="utf-8")
    installer = INSTALL_BACKEND.read_text(encoding="utf-8")
    install_app = (REPO_ROOT / "macos/scripts/install_app.sh").read_text(encoding="utf-8")

    assert '"-P",\n            "-m",' in backend_client
    assert '"-P",\n            "-m",\n            "zen_whisper_mac_backend.probe",' in validator
    assert '"PYTHONSAFEPATH"] = "1"' in environment
    assert '"PYTHONNOUSERSITE"] = "1"' in environment
    assert '"PYTHONDONTWRITEBYTECODE"] = "1"' in environment
    assert 'PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 "$STAGING/bin/python" -P -m zen_whisper_mac_backend.probe runtime-probe' in installer
    assert 'PYTHONSAFEPATH=1 "$PYTHON_PATH" -P -' in installer
    assert 'PYTHONSAFEPATH=1 "$PYTHON_PATH" -P "$BACKEND_DIR/src/zen_whisper_mac_backend/probe.py" wheel-hash' in install_app
    assert '"$PYTHON_PATH" - ' not in installer
    assert '"$PYTHON_PATH" - ' not in install_app


def test_python_dash_p_prevents_cwd_module_shadowing(tmp_path: Path) -> None:
    shadow_pkg = tmp_path / "zen_whisper_mac_backend"
    shadow_pkg.mkdir()
    (shadow_pkg / "__init__.py").write_text(
        'BACKEND_VERSION = "shadow"\nPROTOCOL_VERSION = 999\n',
        encoding="utf-8",
    )
    (shadow_pkg / "server.py").write_text(
        'print("shadow backend loaded")\n',
        encoding="utf-8",
    )

    unsafe = subprocess.run(
        [sys.executable, "-m", "zen_whisper_mac_backend.server"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    assert unsafe.stdout.strip() == "shadow backend loaded"

    safe = subprocess.run(
        [sys.executable, "-P", "-m", "zen_whisper_mac_backend.server"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "macos/backend/src")},
        text=True,
        capture_output=True,
    )
    assert safe.returncode != 0
    assert "usage:" in safe.stderr
    assert "shadow backend loaded" not in safe.stdout
