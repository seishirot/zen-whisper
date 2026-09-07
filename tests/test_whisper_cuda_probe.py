"""CUDA menu probes must not initialize the native runtime in the UI thread."""

import builtins
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.asr import whisper


@pytest.fixture(autouse=True)
def clear_probe_cache():
    whisper._windows_cuda_available.cache_clear()
    yield
    whisper._windows_cuda_available.cache_clear()


def test_windows_menu_probe_is_isolated_and_cached(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in ("ctranslate2", "torch"):
            pytest.fail("The UI capability probe imported a native CUDA runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(whisper.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="1\n"))
    monkeypatch.setattr(subprocess, "run", run)

    assert whisper.is_cuda_available()
    assert whisper.is_cuda_available()
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0][:3] == [whisper.sys.executable, "-I", "-c"]
    assert kwargs["timeout"] == 10
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize("returncode, output", [(0, "0\n"), (1, "1\n"), (0, "unexpected")])
def test_unsuccessful_probe_does_not_offer_cuda(monkeypatch, returncode, output):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=returncode, stdout=output,
    )))
    assert not whisper._windows_cuda_available()


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("probe", 10)])
def test_probe_failure_is_safe_for_menu_construction(monkeypatch, error):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    assert not whisper._windows_cuda_available()
