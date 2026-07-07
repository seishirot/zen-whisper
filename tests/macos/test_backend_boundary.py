from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src/zen_whisper_mac_backend"
SWIFT_SRC = REPO_ROOT / "macos/ZenWhisper/ZenWhisper"


def test_macos_backend_does_not_import_root_src() -> None:
    for path in BACKEND_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from src" not in text
        assert "import src" not in text


def test_swift_host_does_not_launch_root_python_modules() -> None:
    if not SWIFT_SRC.exists():
        return
    forbidden = [
        "src/main.py",
        "src.hotkey",
        "src.recorder",
        "src.paster",
        "src.platform",
        "uv run zen-whisper",
    ]
    for path in SWIFT_SRC.rglob("*.swift"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text
