"""GPU identity, resource admission and UI persistence contracts."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import gpu
from src.config import AppConfig, RecognitionConfig, load_config, save_config

GPU0 = gpu.NvidiaGPU(0, "GPU-00000000-0000-0000-0000-000000000000", "Test NVIDIA 16GB", 16384, 16000)
GPU1 = gpu.NvidiaGPU(1, "GPU-00000000-0000-0000-0000-000000000001", "Test NVIDIA 8GB", 8192, 7800)


def test_named_selection_survives_index_reorder_and_never_falls_back():
    cfg = RecognitionConfig(cuda_gpu_uuid=GPU1.uuid)
    assert gpu.select_gpu(cfg, [replace(GPU1, index=0), replace(GPU0, index=1)]).uuid == GPU1.uuid
    with pytest.raises(gpu.GPUSelectionError, match="見つかりません"):
        gpu.select_gpu(cfg, [GPU0])
    assert gpu.select_gpu(RecognitionConfig(engine="crispasr", crispasr_gpu_device=1), [GPU0, GPU1]) == GPU1


@pytest.mark.parametrize("identity", [False, 1, None, "GPU-bogus"])
def test_invalid_identity_cannot_select_default_gpu(identity):
    with pytest.raises(gpu.GPUSelectionError):
        gpu.select_gpu(RecognitionConfig(cuda_gpu_uuid=identity), [GPU0, GPU1])


def test_q8_can_be_admitted_on_an_empty_8gb_card_but_not_a_busy_one(monkeypatch):
    cfg = RecognitionConfig(engine="crispasr", device="cuda", cuda_gpu_uuid=GPU1.uuid, crispasr_model="qwen3-1.7b-q8")
    monkeypatch.setattr(gpu, "list_nvidia_gpus", lambda: [GPU0, GPU1])
    assert gpu.admit_crispasr(cfg) == GPU1
    monkeypatch.setattr(gpu, "list_nvidia_gpus", lambda: [GPU0, replace(GPU1, free_mib=4800)])
    with pytest.raises(gpu.GPUSelectionError, match="Test NVIDIA 8GB.*4.7 GiB.*7.0 GiB"):
        gpu.admit_crispasr(cfg)


def test_inventory_is_read_only_and_rejects_unknown_memory(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")
    runner = Mock(return_value=SimpleNamespace(stdout=f"1, {GPU1.uuid}, Test NVIDIA 8GB, 8192, 7800\n"))
    monkeypatch.setattr(gpu.subprocess, "run", runner)
    assert gpu.list_nvidia_gpus() == [GPU1]
    assert runner.call_args.kwargs["timeout"] == 3
    runner.return_value.stdout = f"1, {GPU1.uuid}, Test NVIDIA 8GB, 8192, N/A\n"
    with pytest.raises(gpu.GPUSelectionError, match="確認できない"):
        gpu.list_nvidia_gpus()


def test_gpu_selection_roundtrip_and_cuda_only_reload_key(tmp_path):
    from src.main import App
    cfg = AppConfig(recognition=RecognitionConfig(device="cuda", cuda_gpu_uuid=GPU1.uuid))
    path = tmp_path / "config.toml"
    assert save_config(cfg, path)
    assert load_config(path).recognition.cuda_gpu_uuid == GPU1.uuid
    other = AppConfig(recognition=replace(cfg.recognition, cuda_gpu_uuid=GPU0.uuid))
    assert App._recognition_reload_key(cfg) != App._recognition_reload_key(other)
    cfg.recognition.device = other.recognition.device = "cpu"
    assert App._recognition_reload_key(cfg) == App._recognition_reload_key(other)


def test_tray_gpu_handler_uses_uuid_and_keeps_selection_on_rejection():
    from src.tray import TrayApp
    app = TrayApp(lambda lang: True, lambda *args: True, lambda: None,
                  initial_gpu_uuid=GPU0.uuid, on_set_gpu=lambda identity: False)
    app._set_gpu(GPU1.uuid)(None, None)
    assert app._gpu_uuid == GPU0.uuid
    app._on_set_gpu = lambda identity: identity == GPU1.uuid
    app._set_gpu(GPU1.uuid)(None, None)
    assert app._gpu_uuid == GPU1.uuid


def test_settings_refresh_keeps_uuid_even_when_free_memory_changes(monkeypatch):
    import tkinter as tk
    from src.settings import SettingsSnapshot, SettingsWindow
    cfg = AppConfig(recognition=RecognitionConfig(cuda_gpu_uuid=GPU1.uuid))
    window = SettingsWindow(lambda: SettingsSnapshot(cfg, {}, {}), lambda *a: (True, ""), lambda *a: (True, ""), lambda *a: (True, ""))
    key = "recognition.cuda_gpu_label"
    window._vars[key] = tk.StringVar(master=tk.Tcl())
    window._widgets[key] = Mock()
    monkeypatch.setattr(gpu, "list_nvidia_gpus", lambda: [GPU0, GPU1])
    window._refresh_gpu_choices(cfg)
    assert window._config_form_value(key) == GPU1.uuid
    monkeypatch.setattr(gpu, "list_nvidia_gpus", lambda: [GPU0, replace(GPU1, free_mib=4500)])
    window._refresh_gpu_choices()
    assert window._config_form_value(key) == GPU1.uuid
    assert "4.4 GiB" in window._vars[key].get()


def test_cleanup_failure_retains_backend_and_blocks_replacement(monkeypatch):
    from src.transcriber import Transcriber
    transcriber = Transcriber()
    backend = SimpleNamespace(is_ready=True, unload=Mock(side_effect=RuntimeError("cannot reap")))
    transcriber._backend = backend
    create = Mock()
    monkeypatch.setattr(transcriber, "_create_backend", create)
    with pytest.raises(RuntimeError, match="cannot reap"):
        transcriber.load_model(RecognitionConfig())
    assert not create.called and not transcriber.is_ready
    assert transcriber._backend is backend
    backend.unload.side_effect = None
    transcriber.unload()
    assert transcriber._backend is None
