"""文字起こしモジュール。ASR バックエンド選択と共通ログを担当する。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Lock

import numpy as np

from src.asr.base import ASRBackend
from src.asr.qwen import Qwen3Backend, is_qwen3_available
from src.asr.reazon import ReazonK2Backend, is_reazon_k2_available
from src.asr.whisper import (
    FasterWhisperBackend,
    MlxWhisperBackend,
    _resolve_device,
    _to_mlx_repo,
    is_cuda_available as is_whisper_cuda_available,
)
from src.config import (
    ASR_SAMPLE_RATE,
    ENGINE_AUTO,
    ENGINE_QWEN3_ASR,
    ENGINE_REAZON_K2,
    ENGINE_WHISPER,
    RecognitionConfig,
)

logger = logging.getLogger(__name__)


def _resolve_engine(cfg: RecognitionConfig) -> str:
    """Resolve the requested engine to a concrete backend family."""
    if cfg.engine == ENGINE_AUTO:
        logger.warning("engine='auto' は非推奨です。Whisper として扱います")
        return ENGINE_WHISPER
    return cfg.engine


class Transcriber:
    """ASR エンジンをロードし、16kHz mono 音声を文字起こしする。"""

    def __init__(self) -> None:
        self._backend: ASRBackend | None = None
        self._engine: str = ""
        self._lock = Lock()

    def load_model(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        """モデルをロードする。バックグラウンドスレッドから呼び出すことを想定。"""
        with self._lock:
            self._backend = None
            self._engine = ""

        engine = _resolve_engine(cfg)
        backend = self._create_backend(engine, cfg)
        logger.info(
            "ASR バックエンドをロード中: requested=%s, resolved=%s",
            cfg.engine,
            backend.name,
        )
        try:
            backend.load(cfg, on_timeout)
        except Exception:
            raise

        if backend.is_ready:
            with self._lock:
                self._backend = backend
                self._engine = backend.name

    def _create_backend(self, engine: str, cfg: RecognitionConfig) -> ASRBackend:
        if engine == ENGINE_REAZON_K2:
            return ReazonK2Backend()
        if engine == ENGINE_QWEN3_ASR:
            return Qwen3Backend()
        if engine == ENGINE_WHISPER:
            resolved = _resolve_device(cfg)
            logger.info(
                "Whisper デバイスを解決: model=%s, device=%s (resolved=%s)",
                cfg.model_size,
                cfg.device,
                resolved,
            )
            if resolved == "mlx":
                return MlxWhisperBackend()
            return FasterWhisperBackend(resolved)
        raise ValueError(f"unknown ASR engine: {engine}")

    @property
    def is_ready(self) -> bool:
        return self._backend is not None and self._backend.is_ready

    @property
    def engine_label(self) -> str:
        return self._engine or "unloaded"

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig | None = None,
    ) -> str:
        """
        音声データ (float32, 16kHz, mono) を文字起こしする。

        Args:
            audio: 音声データ配列
            language: 言語コード ("ja" or "en")
            cfg: 認識設定（None の場合はデフォルト値を使用）

        Returns:
            認識テキスト
        """
        if cfg is None:
            cfg = RecognitionConfig()

        audio_duration = len(audio) / ASR_SAMPLE_RATE

        with self._lock:
            backend = self._backend
            engine = self._engine
            if backend is None or not backend.is_ready:
                logger.error("モデルがロードされていません")
                return ""

            t0 = time.perf_counter()
            text = backend.transcribe(audio, language, cfg)
            elapsed = time.perf_counter() - t0

        rtf = elapsed / audio_duration if audio_duration > 0 else 0
        logger.info(
            "文字起こし完了 (%s): lang=%s, 文字数=%d, 音声=%.1f秒, 処理=%.2f秒, RTF=%.3f",
            engine,
            language,
            len(text),
            audio_duration,
            elapsed,
            rtf,
        )
        if text:
            logger.debug("認識結果: %s", text)
        return text
