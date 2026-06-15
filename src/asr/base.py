"""Common helpers for ASR backends."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

import numpy as np

from src.config import RecognitionConfig

logger = logging.getLogger(__name__)


class ASRBackend(Protocol):
    """Minimal interface implemented by each ASR backend."""

    name: str

    @property
    def is_ready(self) -> bool:
        """Return whether the backend finished loading."""

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        """Load model resources."""

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
    ) -> str:
        """Transcribe 16kHz mono float32 audio."""


def load_with_timeout(
    target: Callable[[], object],
    timeout_sec: int,
    engine_label: str,
    on_timeout: Callable[[str], None] | None = None,
) -> object | None:
    """Run target in a background thread and wait up to timeout_sec."""
    result: list[object] = []
    error: list[Exception] = []

    def _run() -> None:
        try:
            result.append(target())
        except Exception as e:
            error.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        msg = f"{engine_label} モデルのロードが {timeout_sec}秒 でタイムアウトしました"
        logger.error(msg)
        if on_timeout:
            on_timeout(msg)
        return None

    if error:
        raise error[0]

    return result[0] if result else None
