"""Common helpers for ASR backends."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from src.config import RecognitionConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecognitionHints:
    """Backend-neutral context supplied by a selected domain profile."""

    context: str = ""
    hotwords: tuple[str, ...] = ()


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
        hints: RecognitionHints | None = None,
    ) -> str:
        """Transcribe 16kHz mono float32 audio."""


def load_with_timeout(
    target: Callable[[], object],
    timeout_sec: int,
    engine_label: str,
    on_timeout: Callable[[str], None] | None = None,
) -> object | None:
    """Run one model load synchronously and warn when it exceeds the threshold.

    Python cannot safely cancel native model constructors. Running the heavy
    target in a disposable timeout thread would leave it alive and allow a
    second model load to start concurrently. A lightweight watchdog therefore
    reports the threshold while the caller keeps ownership of the real load.
    """
    done = threading.Event()

    def _watch_timeout() -> None:
        if done.wait(timeout=timeout_sec):
            return
        msg = (
            f"{engine_label} モデルのロードが {timeout_sec}秒 を超えました。"
            "安全のため完了まで待機します"
        )
        logger.error(msg)
        if on_timeout:
            on_timeout(msg)

    watchdog = threading.Thread(target=_watch_timeout, daemon=True)
    watchdog.start()
    try:
        return target()
    finally:
        done.set()
