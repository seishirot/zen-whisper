"""Qwen3-ASR backend."""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable

import numpy as np

from src.asr.base import load_with_timeout
from src.asr.whisper import _resolve_device
from src.config import ASR_SAMPLE_RATE, RecognitionConfig

logger = logging.getLogger(__name__)

_QWEN3_LANG_MAP: dict[str, str] = {"ja": "Japanese", "en": "English"}
_qwen3_available: bool | None = None


def is_qwen3_available() -> bool:
    """Return whether qwen_asr is importable."""
    global _qwen3_available
    if _qwen3_available is None:
        try:
            import qwen_asr  # noqa: F401

            _qwen3_available = True
        except ImportError:
            _qwen3_available = False
    return _qwen3_available


def _resolve_qwen3_attn(requested: str) -> str:
    requested = (requested or "auto").lower()
    if requested != "auto":
        return requested
    try:
        from transformers.utils import is_flash_attn_2_available

        if is_flash_attn_2_available():
            return "flash_attention_2"
    except Exception:
        pass
    return "sdpa"


def _is_triton_available() -> bool:
    return importlib.util.find_spec("triton") is not None


class Qwen3Backend:
    """Qwen3-ASR backend."""

    name = "qwen3-asr"

    def __init__(self) -> None:
        self._model = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        resolved = _resolve_device(cfg)
        attn_impl = _resolve_qwen3_attn(cfg.qwen3_attn_implementation)
        use_compile = (
            cfg.qwen3_torch_compile and resolved != "cpu" and _is_triton_available()
        )
        if cfg.qwen3_torch_compile and not use_compile:
            logger.info(
                "torch.compile は無効です（triton 未導入のため）。attn=%s で実行します",
                attn_impl,
            )

        def factory() -> object:
            from qwen_asr import Qwen3ASRModel
            import torch

            asr_model = Qwen3ASRModel.from_pretrained(
                cfg.qwen3_model,
                dtype=torch.bfloat16,
                device_map=resolved if resolved != "cpu" else "cpu",
                max_new_tokens=cfg.qwen3_max_new_tokens,
                attn_implementation=attn_impl,
            )

            if use_compile:
                logger.info("torch.compile() を適用中...")
                asr_model.model = torch.compile(
                    asr_model.model, mode="reduce-overhead"
                )
                logger.info("torch.compile() の適用が完了しました")

            return asr_model

        model = load_with_timeout(
            factory,
            cfg.model_load_timeout_sec,
            "Qwen3-ASR",
            on_timeout,
        )
        if model is not None:
            self._model = model
            compile_label = " [torch.compile]" if use_compile else ""
            logger.info(
                "Qwen3-ASR モデルのロードが完了しました%s (attn=%s, max_new_tokens=%d)",
                compile_label,
                attn_impl,
                cfg.qwen3_max_new_tokens,
            )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
    ) -> str:
        if self._model is None:
            logger.error("モデルがロードされていません")
            return ""
        lang = _QWEN3_LANG_MAP.get(language, "Japanese")
        results = self._model.transcribe(audio=(audio, ASR_SAMPLE_RATE), language=lang)
        return results[0].text.strip()
