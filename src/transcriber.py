"""文字起こしモジュール。ASR バックエンド選択と共通ログを担当する。"""

from __future__ import annotations

import gc
import logging
import sys
import time
from collections.abc import Callable
from threading import Lock

import numpy as np

from src.asr.base import ASRBackend, RecognitionHints
from src.asr.qwen import (
    Qwen3Backend,
    is_qwen3_available,
    is_qwen3_cuda_available,
)
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
    QWEN3_LEGACY_MODELS,
    RecognitionConfig,
)
from src.platform import is_mac

logger = logging.getLogger(__name__)


def _resolve_engine(cfg: RecognitionConfig) -> str:
    """Resolve the requested engine to a concrete backend family."""
    if cfg.engine == ENGINE_AUTO:
        logger.warning("engine='auto' は非推奨です。Whisper として扱います")
        return ENGINE_WHISPER
    return cfg.engine


def available_recognition_engines() -> tuple[str, ...]:
    """Return model families currently usable by this Python installation."""
    engines = [ENGINE_WHISPER]
    if not is_mac() and is_reazon_k2_available():
        engines.append(ENGINE_REAZON_K2)
    if not is_mac() and is_qwen3_available():
        engines.append(ENGINE_QWEN3_ASR)
    return tuple(engines)


def available_recognition_devices(engine: str) -> tuple[str, ...]:
    """Return execution targets supported by an installed engine."""
    if engine == ENGINE_REAZON_K2:
        return ("cpu",)
    if engine == ENGINE_QWEN3_ASR:
        devices = ["cpu"]
        if is_qwen3_cuda_available():
            devices.insert(0, "cuda")
        return tuple(devices)
    if is_mac():
        return ("mlx",)
    devices = ["cpu"]
    if is_whisper_cuda_available():
        devices.insert(0, "cuda")
    return tuple(devices)


def recognition_configuration_error(cfg: RecognitionConfig) -> str:
    """Describe why a requested engine/device cannot be loaded right now."""
    if cfg.engine not in available_recognition_engines():
        if cfg.engine == ENGINE_REAZON_K2:
            return (
                "Reazon K2 が未導入です。uv sync --locked --extra reazon を実行して"
                "ZenWhisperを再起動してください"
            )
        if cfg.engine == ENGINE_QWEN3_ASR:
            return (
                "Qwen3-ASR が未導入です。qwen3 または qwen3-cuda extraを"
                "導入してZenWhisperを再起動してください"
            )
        return f"認識エンジン {cfg.engine} はこの環境で使用できません"

    if (
        cfg.engine == ENGINE_QWEN3_ASR
        and cfg.qwen3_model in QWEN3_LEGACY_MODELS
    ):
        return (
            "選択中のQwen3-ASRモデルは旧qwen-asr形式です。"
            "末尾が -hf のモデルを選び直してください"
        )

    devices = available_recognition_devices(cfg.engine)
    if cfg.device not in devices:
        available = " / ".join(devices)
        return (
            f"{cfg.engine} の実行先 {cfg.device} はこの環境で使用できません。"
            f"利用可能: {available}"
        )
    return ""


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
        self.unload()

        engine = _resolve_engine(cfg)
        backend = self._create_backend(engine, cfg)
        with self._lock:
            # Own the backend before loading so a partial allocation can still
            # be cleaned if backend.load() raises.
            self._backend = backend
            self._engine = ""
        logger.info(
            "ASR バックエンドをロード中: requested=%s, resolved=%s",
            cfg.engine,
            backend.name,
        )
        try:
            backend.load(cfg, on_timeout)
        except Exception:
            self.unload()
            raise

        if backend.is_ready:
            with self._lock:
                if self._backend is backend:
                    self._engine = backend.name
        else:
            self.unload()

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

    def unload(self) -> None:
        """Detach the active backend and release heavyweight model resources."""
        with self._lock:
            backend = self._backend
            self._backend = None
            self._engine = ""

        if backend is not None:
            cleanup = getattr(backend, "unload", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    logger.exception("ASR バックエンドの明示解放に失敗しました")
            del backend
        gc.collect()

        # Do not import optional runtimes just for cleanup. If PyTorch/MLX is
        # already loaded, ask its allocator to return now-unused cached memory.
        torch_module = sys.modules.get("torch")
        cuda = getattr(torch_module, "cuda", None)
        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            try:
                empty_cache()
            except Exception:
                logger.debug("PyTorch CUDA キャッシュの解放に失敗しました", exc_info=True)

        mlx_core = sys.modules.get("mlx.core")
        clear_cache = getattr(mlx_core, "clear_cache", None)
        if callable(clear_cache):
            try:
                clear_cache()
            except Exception:
                logger.debug("MLX キャッシュの解放に失敗しました", exc_info=True)

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig | None = None,
        hints: RecognitionHints | None = None,
    ) -> str:
        """
        音声データ (float32, 16kHz, mono) を文字起こしする。

        Args:
            audio: 音声データ配列
            language: 言語コード ("ja" or "en")
            cfg: 認識設定（None の場合はデフォルト値を使用）
            hints: 選択プロファイルから生成した認識ヒント

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

            logger.info(
                "文字起こし開始 (%s): lang=%s, 音声=%.1f秒",
                engine,
                language,
                audio_duration,
            )
            t0 = time.perf_counter()
            text = backend.transcribe(audio, language, cfg, hints)
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
            logger.debug("認識結果メタデータ: 文字数=%d", len(text))
        return text
