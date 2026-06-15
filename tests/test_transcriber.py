"""src.transcriber のテスト。"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from src.config import ENGINE_REAZON_K2, ENGINE_WHISPER, RecognitionConfig
from src.transcriber import Transcriber, _resolve_device, _resolve_engine, _to_mlx_repo


class TestResolveDevice:
    """デバイス解決ロジックのテスト。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_auto_on_windows_with_cuda(self):
        cfg = RecognitionConfig(device="auto")
        with patch("src.asr.whisper._cuda_available", return_value=True):
            assert _resolve_device(cfg) == "cuda"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_auto_on_windows_without_cuda(self):
        cfg = RecognitionConfig(device="auto")
        with patch("src.asr.whisper._cuda_available", return_value=False):
            assert _resolve_device(cfg) == "cpu"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_auto_on_mac(self):
        cfg = RecognitionConfig(device="auto")
        assert _resolve_device(cfg) == "mlx"

    def test_explicit_cuda(self):
        cfg = RecognitionConfig(device="cuda")
        assert _resolve_device(cfg) == "cuda"

    def test_explicit_cpu(self):
        cfg = RecognitionConfig(device="cpu")
        assert _resolve_device(cfg) == "cpu"

    def test_explicit_mlx(self):
        cfg = RecognitionConfig(device="mlx")
        assert _resolve_device(cfg) == "mlx"

    def test_case_insensitive(self):
        cfg = RecognitionConfig(device="CUDA")
        assert _resolve_device(cfg) == "cuda"

    def test_auto_with_mocked_darwin(self):
        cfg = RecognitionConfig(device="auto")
        with patch("src.asr.whisper.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _resolve_device(cfg) == "mlx"

    def test_auto_with_mocked_win32(self):
        cfg = RecognitionConfig(device="auto")
        with patch("src.asr.whisper.sys") as mock_sys, patch(
            "src.asr.whisper._cuda_available", return_value=True
        ):
            mock_sys.platform = "win32"
            assert _resolve_device(cfg) == "cuda"

    def test_auto_with_mocked_win32_without_cuda(self):
        cfg = RecognitionConfig(device="auto")
        with patch("src.asr.whisper.sys") as mock_sys, patch(
            "src.asr.whisper._cuda_available", return_value=False
        ):
            mock_sys.platform = "win32"
            assert _resolve_device(cfg) == "cpu"


class TestResolveEngine:
    """ASR エンジン解決テスト。"""

    def test_explicit_engine(self):
        cfg = RecognitionConfig(engine=ENGINE_REAZON_K2)
        assert _resolve_engine(cfg) == ENGINE_REAZON_K2

    def test_legacy_auto_uses_whisper_family(self):
        cfg = RecognitionConfig(engine="auto")
        assert _resolve_engine(cfg) == ENGINE_WHISPER

    def test_legacy_auto_does_not_fall_back_to_reazon(self):
        cfg = RecognitionConfig(engine="auto")
        with patch("src.transcriber.is_reazon_k2_available", return_value=True):
            assert _resolve_engine(cfg) == ENGINE_WHISPER


class TestToMlxRepo:
    """MLX リポジトリ名マッピングのテスト。"""

    def test_large_v3_turbo(self):
        assert _to_mlx_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"

    def test_turbo_alias(self):
        assert _to_mlx_repo("turbo") == "mlx-community/whisper-large-v3-turbo"

    def test_common_sizes(self):
        assert _to_mlx_repo("tiny") == "mlx-community/whisper-tiny"
        assert _to_mlx_repo("base") == "mlx-community/whisper-base"
        assert _to_mlx_repo("small") == "mlx-community/whisper-small"
        assert _to_mlx_repo("medium") == "mlx-community/whisper-medium"
        assert _to_mlx_repo("large") == "mlx-community/whisper-large-v3"
        assert _to_mlx_repo("large-v2") == "mlx-community/whisper-large-v2"
        assert _to_mlx_repo("large-v3") == "mlx-community/whisper-large-v3"

    def test_already_repo_name(self):
        repo = "mlx-community/whisper-large-v3-turbo"
        assert _to_mlx_repo(repo) == repo

    def test_unknown_falls_back(self):
        result = _to_mlx_repo("nonexistent-model")
        assert result == "mlx-community/whisper-large-v3-turbo"


class TestTranscriber:
    """Transcriber クラスの基本テスト。"""

    def test_initial_state(self):
        t = Transcriber()
        assert t.is_ready is False
        assert t._engine == ""

    def test_transcribe_without_model(self):
        t = Transcriber()
        import numpy as np
        dummy = np.zeros(16000, dtype=np.float32)
        result = t.transcribe(dummy, "ja")
        assert result == ""
