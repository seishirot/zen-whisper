"""Haas-ja on the macOS backend: registry entry, adapter and Windows parity."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "macos/backend/src"
sys.path.insert(0, str(BACKEND_SRC))

import zen_whisper_mac_backend.adapters as adapters_module  # noqa: E402
import zen_whisper_mac_backend.haas as haas_module  # noqa: E402
from zen_whisper_mac_backend.adapters import (  # noqa: E402
    AdapterError,
    HaasJaAdapter,
    _safe_exception_summary,
    make_adapter,
)
from zen_whisper_mac_backend.haas import (  # noqa: E402
    _Decoding,
    _Detokenizer,
    _tdt_greedy_decode,
    split_at_quiet_points,
    transcribe_audio,
)
from zen_whisper_mac_backend.registry import RegistryError, load_registry  # noqa: E402
from zen_whisper_mac_backend.service import BackendService  # noqa: E402

HAAS_REPO = "kizuna-intelligence/Haas-ja-v1.0-OneCompression-ONNX"


def test_registry_lists_haas_ja_for_japanese_only() -> None:
    registry = load_registry()

    assert "haas-ja" in registry.engine_ids()
    assert registry.default_model("haas-ja") == HAAS_REPO
    assert registry.language_for_engine("ja", "haas-ja") == "ja"
    for language in ("auto", "en"):
        with pytest.raises(RegistryError):
            registry.language_for_engine(language, "haas-ja")
    assert registry.default_engine == "mlx-whisper"


def test_registry_haas_files_match_the_windows_manifest() -> None:
    manifest = json.loads((REPO_ROOT / "src/model_manifest.json").read_text(encoding="utf-8"))
    windows = manifest["groups"]["haas_onnx"]["ja"]
    model = load_registry().model("haas-ja", HAAS_REPO)

    assert windows["repo_id"] == HAAS_REPO
    assert model["revision"] == windows["revision"]
    assert set(model["allow_patterns"]) == set(model["files"])
    assert {"LICENSE", "NOTICE", "int8/encoder.onnx", "int8/encoder.onnx.data"} <= set(model["files"])
    assert not any(name.startswith("int4") for name in model["files"])
    for name, expected in model["files"].items():
        assert dict(expected) == windows["files"][name]


def test_make_adapter_returns_haas_without_hint_support() -> None:
    adapter = make_adapter("haas-ja")

    assert isinstance(adapter, HaasJaAdapter)
    assert not hasattr(adapter, "transcribe_with_hints")


def test_public_model_id_survives_diagnostic_sanitizing() -> None:
    summary = _safe_exception_summary(
        RuntimeError(f"{HAAS_REPO} failed at /Users/me/.cache/huggingface/hub/x/encoder.onnx")
    )

    assert HAAS_REPO in summary
    assert "/Users/me" not in summary


class _FakeModel:
    def __init__(self) -> None:
        self.segments: list[int] = []

    def transcribe_segment(self, audio: np.ndarray) -> str:
        self.segments.append(audio.size)
        return f"[{len(self.segments)}]"


def _install_fake_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    loads: list[object] = []

    def fake_snapshot(engine_id: str, model_id: str) -> str:
        assert (engine_id, model_id) == ("haas-ja", HAAS_REPO)
        return str(tmp_path)

    def fake_model(snapshot: Path, *, threads: int) -> _FakeModel:
        assert snapshot == tmp_path
        assert 1 <= threads <= 4
        model = _FakeModel()
        loads.append(model)
        return model

    monkeypatch.setattr(adapters_module, "_pinned_model_snapshot", fake_snapshot)
    monkeypatch.setattr(haas_module, "HaasModel", fake_model)
    return loads


def test_adapter_loads_once_and_transcribes_wav(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loads = _install_fake_model(monkeypatch, tmp_path)
    audio_path = tmp_path / "audio.wav"
    rng = np.random.default_rng(0)
    sf.write(audio_path, (rng.standard_normal(16000) * 0.1).astype(np.float32), 16000)
    adapter = HaasJaAdapter()

    adapter.preload(HAAS_REPO, "ja")
    text = adapter.transcribe(audio_path, HAAS_REPO, "ja")

    assert text == "[1]"
    assert len(loads) == 1


def test_adapter_reports_unreadable_audio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_model(monkeypatch, tmp_path)
    wrong_rate = tmp_path / "audio.wav"
    sf.write(wrong_rate, np.ones(8000, dtype=np.float32) * 0.1, 8000)
    adapter = HaasJaAdapter()

    with pytest.raises(AdapterError) as missing:
        adapter.transcribe(tmp_path / "missing.wav", HAAS_REPO, "ja")
    with pytest.raises(AdapterError) as resampled:
        adapter.transcribe(wrong_rate, HAAS_REPO, "ja")

    assert missing.value.kind == "audio_unreadable"
    assert resampled.value.kind == "audio_unreadable"


def test_adapter_rejects_non_finite_audio_as_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loads = _install_fake_model(monkeypatch, tmp_path)
    audio = np.full(16000, 0.1, dtype=np.float32)
    audio[100] = np.nan
    audio_path = tmp_path / "nan.wav"
    sf.write(audio_path, audio, 16000, subtype="FLOAT")

    with pytest.raises(AdapterError) as error:
        HaasJaAdapter().transcribe(audio_path, HAAS_REPO, "ja")

    assert error.value.kind == "audio_unreadable"
    assert loads[0].segments == []


def test_service_releases_haas_model_when_switching_engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_model(monkeypatch, tmp_path)
    haas = HaasJaAdapter()
    service = BackendService(adapters={"haas-ja": haas})
    haas.preload(HAAS_REPO, "ja")

    service._adapter("haas-ja")
    assert haas._model is not None
    service._adapter("dummy")
    assert haas._model is None
    service.shutdown(wait=True)


def test_adapter_load_failure_is_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_snapshot(engine_id: str, model_id: str) -> str:
        raise RegistryError(f"Model file hash mismatch: {model_id}/int8/encoder.onnx")

    monkeypatch.setattr(adapters_module, "_pinned_model_snapshot", failing_snapshot)

    with pytest.raises(AdapterError) as error:
        HaasJaAdapter().preload(HAAS_REPO, "ja")

    assert error.value.kind == "model_unavailable"
    assert str(error.value) == "Haas-ja unavailable"


def test_transcribe_audio_skips_empty_and_silent_input() -> None:
    model = _FakeModel()

    assert transcribe_audio(model.transcribe_segment, np.zeros(0, dtype=np.float32)) == ""
    assert transcribe_audio(model.transcribe_segment, np.zeros(32000, dtype=np.float32)) == ""
    assert transcribe_audio(model.transcribe_segment, np.ones(100, dtype=np.float32)) == ""
    assert model.segments == []


def test_transcribe_audio_joins_quiet_point_chunks_in_order() -> None:
    rng = np.random.default_rng(1)
    audio = (rng.standard_normal(16000 * 70) * 0.1).astype(np.float32)
    model = _FakeModel()

    text = transcribe_audio(model.transcribe_segment, audio)

    bounds = split_at_quiet_points(audio, chunk_seconds=haas_module.CHUNK_SECONDS)
    assert len(bounds) == 3
    assert text == "[1][2][3]"
    assert sum(model.segments) == audio.size


def test_split_at_quiet_points_prefers_silence_near_the_boundary() -> None:
    audio = np.ones(16000 * 40, dtype=np.float32)
    quiet_start = 16000 * 29
    audio[quiet_start : quiet_start + 1600] = 0

    bounds = split_at_quiet_points(audio, chunk_seconds=28.0)

    assert bounds[0][0] == 0
    assert quiet_start <= bounds[0][1] <= quiet_start + 1600
    assert bounds[-1][1] == audio.size


def test_tdt_decode_and_detokenizer_produce_utf8_text() -> None:
    cfg = _Decoding(blank_id=3, durations=(0, 1), max_symbols=2, num_tokens=3)
    # pieces: 0="▁あ", 1=<0xE3>, 2=<0x81>, 3=blank, 4=<0x84> (detokenizer only)
    pieces = ("▁あ", "<0xE3>", "<0x81>", "<blank>", "<0x84>")
    script = iter([0, 1, 2, 3])  # one emission per frame, blank on the last

    def joint_at(t: int, pred: np.ndarray) -> np.ndarray:
        logits = np.zeros(cfg.num_tokens + 1 + len(cfg.durations), dtype=np.float32)
        logits[next(script)] = 1.0
        logits[cfg.num_tokens + 1 + 1] = 1.0  # duration 1
        return logits

    ids = _tdt_greedy_decode(4, joint_at, lambda token, state: (None, state), (None, None), cfg)
    assert ids == [0, 1, 2]
    detok = _Detokenizer(pieces, unk_id=99, add_dummy_prefix=True)

    assert detok.decode([0, 1, 2, 4]) == "あい"
    assert detok.decode([1]) == "�"


_SHARED_DEFINITIONS = (
    "_Frontend",
    "_load_frontend",
    "_log_mel",
    "_Decoding",
    "_tdt_greedy_decode",
    "_decode_bytes",
    "_Detokenizer",
    "split_at_quiet_points",
)


class _Normalize(ast.NodeTransformer):
    """Ignore message wording and the sample-rate constant's module-local name."""

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value="<str>"), node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return ast.copy_location(ast.Constant(value="<str>"), node)

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == "ASR_SAMPLE_RATE":
            node.id = "SAMPLE_RATE"
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
            getattr(node.body[0], "value", None), ast.Constant
        ):
            node.body = node.body[1:]
        self.generic_visit(node)
        return node

    visit_ClassDef = visit_FunctionDef  # type: ignore[assignment]


def _definitions(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _SHARED_DEFINITIONS:
            found[node.name] = ast.dump(_Normalize().visit(node))
    return found


def _method_bodies(path: Path, class_name: str, methods: dict[str, str]) -> dict[str, list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    found: dict[str, list[str]] = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name in methods:
            normalized = _Normalize().visit(node)
            found[methods[node.name]] = [ast.dump(stmt) for stmt in normalized.body]
    return found


def test_onnx_session_code_matches_the_windows_backend() -> None:
    windows = _method_bodies(
        REPO_ROOT / "src/asr/haas.py",
        "_HaasModel",
        {"__init__": "init", "_predict": "predict", "transcribe": "segment"},
    )
    mac = _method_bodies(
        BACKEND_SRC / "zen_whisper_mac_backend/haas.py",
        "HaasModel",
        {"__init__": "init", "_predict": "predict", "transcribe_segment": "segment"},
    )
    # The macOS port validates its thread count up front; everything after
    # that guard must stay identical to the Windows session setup.
    guard = mac["init"][0]
    assert guard.startswith("If(") and "id='threads'" in guard
    mac["init"] = mac["init"][1:]

    assert set(windows) == {"init", "predict", "segment"}
    for name in windows:
        assert mac[name] == windows[name], f"HaasModel.{name} drifted from src/asr/haas.py"


def test_backend_mit_license_matches_the_repository_license() -> None:
    assert (REPO_ROOT / "macos/backend/LICENSES/MIT.txt").read_bytes() == (
        REPO_ROOT / "LICENSE"
    ).read_bytes()


def test_inference_math_matches_the_windows_backend() -> None:
    windows = _definitions(REPO_ROOT / "src/asr/haas.py")
    mac = _definitions(BACKEND_SRC / "zen_whisper_mac_backend/haas.py")

    assert set(windows) == set(_SHARED_DEFINITIONS)
    for name in _SHARED_DEFINITIONS:
        assert mac[name] == windows[name], f"{name} drifted from src/asr/haas.py"


def test_haas_runtime_dependency_is_in_the_installer_extra() -> None:
    pyproject = (REPO_ROOT / "macos/backend/pyproject.toml").read_text(encoding="utf-8")
    mlx_extra = pyproject.split("mlx = [", 1)[1].split("]", 1)[0]

    assert '"onnxruntime' in mlx_extra
