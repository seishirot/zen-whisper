"""src.platform 抽象化レイヤーのテスト。"""

from __future__ import annotations

import sys
import hashlib
from pathlib import Path

import pytest

from src.platform import is_mac, is_windows, paste_hotkey


class TestOSDetection:
    """OS 判定ユーティリティのテスト。"""

    def test_is_windows_returns_bool(self):
        assert isinstance(is_windows(), bool)

    def test_is_mac_returns_bool(self):
        assert isinstance(is_mac(), bool)

    def test_mutually_exclusive(self):
        """Windows と Mac が同時に True にならない。"""
        assert not (is_windows() and is_mac())

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_is_windows_on_windows(self):
        assert is_windows() is True
        assert is_mac() is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_is_mac_on_mac(self):
        assert is_mac() is True
        assert is_windows() is False


class TestPasteHotkey:
    """ペーストキーのテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_paste_hotkey(self):
        assert paste_hotkey() == ("ctrl", "v")

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_mac_paste_hotkey(self):
        assert paste_hotkey() == ("command", "v")

    def test_returns_tuple_of_two_strings(self):
        result = paste_hotkey()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(s, str) for s in result)


def test_mac_legacy_startup_targets_signed_native_app() -> None:
    from src.platform.darwin import _get_launch_command

    assert _get_launch_command() == ["/usr/bin/open", "/Applications/zen-whisper.app"]


def test_mac_legacy_startup_validates_native_app_bundle() -> None:
    from src.platform import darwin

    source = Path(darwin.__file__).read_text(encoding="utf-8")
    assert '_NATIVE_APP_PATH = Path("/Applications/zen-whisper.app")' in source
    assert '_NATIVE_BUNDLE_ID = "com.seishirot.zenwhisper"' in source
    assert "def _native_app_is_installed() -> bool:" in source
    assert 'data.get("CFBundleIdentifier") != _NATIVE_BUNDLE_ID' in source
    assert '"/usr/bin/codesign", "--verify", "--strict"' in source
    assert '"/usr/bin/codesign", "-dr", "-"' in source
    assert 'baseline.get("executable_sha256")' in source
    assert "def _sha256_hex(path: Path) -> str:" in source


def test_mac_legacy_startup_requires_matching_codesign_baseline(tmp_path, monkeypatch) -> None:
    import json
    import plistlib
    import subprocess
    from types import SimpleNamespace

    from src.platform import darwin

    app_path = tmp_path / "zen-whisper.app"
    contents = app_path / "Contents"
    executable = contents / "MacOS" / "zen-whisper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"app")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.seishirot.zenwhisper"})
    )
    signing_json = tmp_path / "signing.json"
    signing_json.write_text(
        json.dumps(
            {
                "app_path": str(app_path),
                "executable_sha256": hashlib.sha256(b"app").hexdigest(),
                "designated_requirement": 'identifier "com.seishirot.zenwhisper"',
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["/usr/bin/codesign", "--verify", "--strict"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["/usr/bin/codesign", "-dr", "-"]:
            return SimpleNamespace(
                returncode=0,
                stdout='designated => identifier "com.seishirot.zenwhisper"\n',
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(darwin, "_NATIVE_APP_PATH", app_path)
    monkeypatch.setattr(darwin, "_SIGNING_JSON", signing_json)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert darwin._native_app_is_installed() is True
    assert ["/usr/bin/codesign", "--verify", "--strict", str(app_path)] in calls


def test_mac_legacy_startup_removes_plist_when_launchctl_load_fails(tmp_path, monkeypatch) -> None:
    import subprocess

    from src.platform import darwin

    plist_path = tmp_path / "com.zen-whisper.plist"

    def fake_run(args, **kwargs):
        if args[:2] == ["/bin/launchctl", "load"]:
            raise subprocess.CalledProcessError(1, args)
        raise AssertionError(args)

    monkeypatch.setattr(darwin, "_PLIST_PATH", plist_path)
    monkeypatch.setattr(darwin, "_native_app_is_installed", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert darwin.register_startup() is False
    assert not plist_path.exists()
