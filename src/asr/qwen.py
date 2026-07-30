"""Qwen3-ASR backend."""

from __future__ import annotations

import importlib.util
import logging
import re
from collections.abc import Callable
from typing import Any

import numpy as np

from src.asr.base import RecognitionHints, load_with_timeout
from src.asr.whisper import _resolve_device
from src.config import (
    ASR_SAMPLE_RATE,
    QWEN3_LEGACY_MODELS,
    RecognitionConfig,
)

logger = logging.getLogger(__name__)

_QWEN3_LANG_MAP: dict[str, str] = {"ja": "Japanese", "en": "English"}
_QWEN3_MAX_CHUNK_SEC = 60.0
_QWEN3_SEC_PER_OUTPUT_TOKEN = 0.25
_QWEN3_MIN_CHUNK_SEC = 1.0
_QWEN3_CHUNK_OVERLAP_SEC = 1.0
_QWEN3_BOUNDARY_SEARCH_SEC = 5.0
_QWEN3_ENERGY_WINDOW_SEC = 0.1
_QWEN3_MIN_INPUT_SEC = 0.5
_qwen3_available: bool | None = None


def is_qwen3_available() -> bool:
    """Return whether the Transformers-native Qwen3-ASR runtime is importable."""
    global _qwen3_available
    if _qwen3_available is None:
        try:
            import torch  # noqa: F401
            from transformers import (  # noqa: F401
                AutoModelForMultimodalLM,
                AutoProcessor,
                Qwen3ASRForConditionalGeneration,
            )

            _qwen3_available = True
        except Exception as exc:
            logger.warning(
                "Qwen3-ASR の利用可否を確認できません: type=%s",
                type(exc).__name__,
            )
            _qwen3_available = False
    return _qwen3_available


def is_qwen3_cuda_available() -> bool:
    """Return whether the installed Qwen/PyTorch runtime can use CUDA."""
    if not is_qwen3_available():
        return False
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


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


def _build_qwen3_prompt(
    processor: Any,
    context: str,
    language: str,
) -> str:
    """Build the prompt used by Qwen's non-streaming Transformers backend."""
    messages = [
        {"role": "system", "content": context or ""},
        {"role": "user", "content": [{"type": "audio", "audio": ""}]},
    ]
    prompt = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=False,
    )
    if language:
        prompt += f"language {language}<asr_text>"
    return prompt


def _qwen3_chunk_sec(max_new_tokens: int) -> float:
    """Choose a conservative audio chunk length for the output token budget."""
    return min(
        _QWEN3_MAX_CHUNK_SEC,
        max(_QWEN3_MIN_CHUNK_SEC, max_new_tokens * _QWEN3_SEC_PER_OUTPUT_TOKEN),
    )


def _split_qwen3_audio(
    audio: np.ndarray,
    max_chunk_sec: float = _QWEN3_MAX_CHUNK_SEC,
) -> list[np.ndarray]:
    """Split long audio near low-energy boundaries without gaps or overlap."""
    if max_chunk_sec <= 0:
        raise ValueError("max_chunk_sec must be positive")

    waveform = np.asarray(audio, dtype=np.float32)
    if waveform.ndim > 1:
        waveform = np.mean(waveform, axis=-1, dtype=np.float32)
    waveform = waveform.reshape(-1)

    max_samples = max(1, int(max_chunk_sec * ASR_SAMPLE_RATE))
    if len(waveform) <= max_samples:
        return [waveform]

    search_samples = min(
        int(_QWEN3_BOUNDARY_SEARCH_SEC * ASR_SAMPLE_RATE),
        max_samples // 4,
    )
    energy_window = max(4, int(_QWEN3_ENERGY_WINDOW_SEC * ASR_SAMPLE_RATE))
    chunks: list[np.ndarray] = []
    start = 0

    while len(waveform) - start > max_samples:
        target = start + max_samples
        left = max(start, target - search_samples)
        right = target
        segment = np.abs(waveform[left:right])

        if len(segment) <= energy_window:
            boundary = target
        else:
            cumulative = np.concatenate(
                (
                    np.zeros(1, dtype=np.float64),
                    np.cumsum(segment, dtype=np.float64),
                )
            )
            window_energy = cumulative[energy_window:] - cumulative[:-energy_window]
            window_start = int(np.argmin(window_energy))
            quiet_window = segment[window_start : window_start + energy_window]
            boundary = left + window_start + int(np.argmin(quiet_window))

        boundary = min(target, max(start + 1, boundary))
        chunks.append(waveform[start:boundary])
        start = boundary

    chunks.append(waveform[start:])
    return chunks


def _prepare_qwen3_audio_chunks(
    audio: np.ndarray,
    max_chunk_sec: float,
) -> list[np.ndarray]:
    """Create bounded chunks with short overlap for transcript alignment."""
    max_samples = max(1, int(max_chunk_sec * ASR_SAMPLE_RATE))
    overlap_samples = min(
        int(_QWEN3_CHUNK_OVERLAP_SEC * ASR_SAMPLE_RATE),
        max_samples // 4,
    )
    core_samples = max(1, max_samples - overlap_samples)
    cores = _split_qwen3_audio(
        audio,
        max_chunk_sec=core_samples / ASR_SAMPLE_RATE,
    )
    if len(cores) <= 1:
        return cores

    waveform = np.concatenate(cores)
    chunks: list[np.ndarray] = []
    cursor = 0
    for index, core in enumerate(cores):
        start = cursor
        end = start + len(core)
        expanded_start = start if index == 0 else max(0, start - overlap_samples)
        expanded_end = end
        chunks.append(waveform[expanded_start:expanded_end])
        cursor = end
    return chunks


def _matching_text_overlap(left: str, right: str) -> int:
    """Return a reliable exact suffix/prefix overlap length."""
    for size in range(min(len(left), len(right)), 0, -1):
        left_part = left[-size:]
        right_part = right[:size]
        if left_part.casefold() != right_part.casefold():
            continue
        minimum = 2 if any(ord(char) > 127 for char in left_part) else 3
        if size < minimum:
            continue
        if left_part.isascii():
            left_start = len(left) - size
            starts_on_boundary = (
                left_start == 0
                or not (
                    _is_ascii_word_char(left[left_start - 1])
                    and _is_ascii_word_char(left_part[0])
                )
            )
            ends_on_boundary = (
                size == len(right)
                or not (
                    _is_ascii_word_char(right_part[-1])
                    and _is_ascii_word_char(right[size])
                )
            )
            if not (starts_on_boundary and ends_on_boundary):
                continue
        return size
    return 0


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _has_unmatched_straight_quote(text: str, quote: str) -> bool:
    is_open = False
    for index, char in enumerate(text):
        if char != quote:
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        before_word = _is_ascii_word_char(before)
        after_word = _is_ascii_word_char(after)
        if quote == "'" and before_word and after_word:
            continue
        if quote == "'" and before_word and not after_word and not is_open:
            continue
        is_open = not is_open
    return is_open


def _starts_without_space(
    part: str,
    preceding: str,
    following: str | None,
) -> bool:
    first = part[0]
    if first in ".,!?;:%)]}’”-":
        return True
    if (
        preceding.endswith("'")
        and len(preceding) > 1
        and _is_ascii_word_char(preceding[-2])
        and re.match(r"^(?:s|t|re|ve|ll|d|m)\b", part, re.I)
    ):
        return True
    if first not in "'\"":
        return False
    if _has_unmatched_straight_quote(preceding, first):
        return True
    if first == "'" and re.match(r"^'(?:s|t|re|ve|ll|d|m)\b", part, re.I):
        return True
    if len(part) == 1:
        return following is None
    next_non_quote = next(
        (char for char in part[1:] if char not in "'\""),
        "",
    )
    if not next_non_quote:
        return following is None
    return next_non_quote.isspace() or next_non_quote in ".,!?;:%)]}’”-"


def _ends_with_opening_punctuation(text: str) -> bool:
    last = text[-1]
    if last in "([{‘“-":
        return True
    if last not in "'\"":
        return False
    return _has_unmatched_straight_quote(text, last)


def _join_qwen3_transcripts(parts: list[str], language: str) -> str:
    """Align overlapping chunks and preserve language-appropriate boundaries."""
    nonempty = [part.strip() for part in parts if part.strip()]
    result = ""
    for index, part in enumerate(nonempty):
        following = nonempty[index + 1] if index + 1 < len(nonempty) else None
        if not result:
            result = part
            continue
        overlap = _matching_text_overlap(result, part)
        if overlap:
            result += part[overlap:]
        elif language != "English":
            result += part
        elif (
            _starts_without_space(part, result, following)
            or _ends_with_opening_punctuation(result)
        ):
            result += part
        else:
            result += " " + part
    return result


def _generation_ended_normally(model: Any, generated_ids: Any) -> bool:
    """Return whether the generated sequence ends with a configured EOS token."""
    if generated_ids.shape[-1] == 0:
        return True

    generation_config = getattr(model, "generation_config", None)
    eos_token_id = getattr(generation_config, "eos_token_id", None)
    if eos_token_id is None:
        model_config = getattr(model, "config", None)
        eos_token_id = getattr(model_config, "eos_token_id", None)
    if eos_token_id is None:
        return False

    eos_ids = (
        {int(token_id) for token_id in eos_token_id}
        if isinstance(eos_token_id, (list, tuple, set, frozenset))
        else {int(eos_token_id)}
    )
    last_token = generated_ids[0, -1]
    if hasattr(last_token, "item"):
        last_token = last_token.item()
    return int(last_token) in eos_ids


class Qwen3Backend:
    """Qwen3-ASR backend."""

    name = "qwen3-asr"

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._compile_config = None
        self._max_new_tokens = 256

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._processor is not None

    def load(
        self,
        cfg: RecognitionConfig,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        self.unload()
        max_new_tokens = cfg.qwen3_max_new_tokens
        if (
            not isinstance(max_new_tokens, int)
            or isinstance(max_new_tokens, bool)
            or max_new_tokens <= 0
        ):
            raise ValueError("qwen3_max_new_tokens は正の整数である必要があります")
        if cfg.qwen3_model in QWEN3_LEGACY_MODELS:
            raise ValueError(
                "旧qwen-asr形式のモデルは使用できません。"
                "末尾が -hf のモデルを選んでください"
            )

        resolved = _resolve_device(cfg)
        if resolved not in ("cuda", "cpu"):
            raise ValueError(
                f"Qwen3-ASR の実行先は cuda または cpu が必要です: {resolved}"
            )
        attn_impl = _resolve_qwen3_attn(cfg.qwen3_attn_implementation)
        use_compile = (
            cfg.qwen3_torch_compile and resolved != "cpu" and _is_triton_available()
        )
        if cfg.qwen3_torch_compile and not use_compile:
            disable_reason = (
                "CPU 実行のため" if resolved == "cpu" else "triton 未導入のため"
            )
            logger.info(
                "torch.compile は無効です（%s）。attn=%s で実行します",
                disable_reason,
                attn_impl,
            )

        def factory() -> tuple[object, object, object | None]:
            from transformers import (
                AutoModelForMultimodalLM,
                AutoProcessor,
                CompileConfig,
            )
            import torch

            processor = AutoProcessor.from_pretrained(cfg.qwen3_model)
            model = AutoModelForMultimodalLM.from_pretrained(
                cfg.qwen3_model,
                dtype=torch.bfloat16,
                attn_implementation=attn_impl,
            )
            model = model.to(resolved).eval()
            compile_config = (
                CompileConfig(mode="reduce-overhead") if use_compile else None
            )
            return model, processor, compile_config

        resources = load_with_timeout(
            factory,
            cfg.model_load_timeout_sec,
            "Qwen3-ASR",
            on_timeout,
        )
        if resources is not None:
            model, processor, compile_config = resources
            self._model = model
            self._processor = processor
            self._compile_config = compile_config
            self._max_new_tokens = max_new_tokens
            compile_label = " [torch.compile]" if use_compile else ""
            logger.info(
                "Qwen3-ASR モデルのロードが完了しました%s (attn=%s, max_new_tokens=%d)",
                compile_label,
                attn_impl,
                max_new_tokens,
            )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str,
        cfg: RecognitionConfig,
        hints: RecognitionHints | None = None,
    ) -> str:
        if not self.is_ready:
            logger.error("モデルがロードされていません")
            return ""
        if audio.size == 0:
            return ""
        lang = _QWEN3_LANG_MAP.get(language, "Japanese")
        context = hints.context if hints is not None else ""
        max_chunk_sec = _qwen3_chunk_sec(self._max_new_tokens)
        chunks = _prepare_qwen3_audio_chunks(audio, max_chunk_sec)
        if len(chunks) > 1:
            logger.info(
                "Qwen3-ASR 長音声を %d チャンクへ分割します "
                "(入力最大 %.1f秒, overlap=%.1f秒)",
                len(chunks),
                max_chunk_sec,
                _QWEN3_CHUNK_OVERLAP_SEC,
            )
        transcripts: list[str] = []
        for chunk in chunks:
            transcripts.extend(
                self._transcribe_chunk_with_retry(chunk, context, lang)
            )
        return _join_qwen3_transcripts(transcripts, lang)

    def _transcribe_chunk_with_retry(
        self,
        audio: np.ndarray,
        context: str,
        language: str,
    ) -> list[str]:
        text, truncated = self._transcribe_chunk(audio, context, language)
        if not truncated:
            return [text]

        duration = len(audio) / ASR_SAMPLE_RATE
        if duration <= _QWEN3_MIN_CHUNK_SEC:
            raise RuntimeError(
                "Qwen3-ASR の生成上限へ到達しました。"
                f"{duration:.1f}秒まで分割しても文字起こしを完了できません"
            )

        retry_max_sec = max(_QWEN3_MIN_CHUNK_SEC, duration / 2)
        retry_chunks = _prepare_qwen3_audio_chunks(audio, retry_max_sec)
        if len(retry_chunks) <= 1:
            raise RuntimeError(
                "Qwen3-ASR の生成上限到達後に音声を再分割できません"
            )
        logger.warning(
            "Qwen3-ASR の生成が上限へ到達したため、"
            "音声を %d チャンクへ再分割します "
            "(audio=%.1f秒, retry_max=%.1f秒, max_new_tokens=%d)",
            len(retry_chunks),
            duration,
            retry_max_sec,
            self._max_new_tokens,
        )
        transcripts: list[str] = []
        for chunk in retry_chunks:
            transcripts.extend(
                self._transcribe_chunk_with_retry(chunk, context, language)
            )
        return transcripts

    def _transcribe_chunk(
        self,
        audio: np.ndarray,
        context: str,
        language: str,
    ) -> tuple[str, bool]:
        model = self._model
        processor = self._processor
        if model is None or processor is None:
            raise RuntimeError("Qwen3-ASR model is not ready")

        min_samples = int(_QWEN3_MIN_INPUT_SEC * ASR_SAMPLE_RATE)
        if len(audio) < min_samples:
            audio = np.pad(audio, (0, min_samples - len(audio))).astype(np.float32)

        prompt = _build_qwen3_prompt(processor, context, language)
        inputs = processor(
            text=[prompt],
            audio=[audio],
            return_tensors="pt",
            padding=True,
        )
        inputs = inputs.to(model.device, model.dtype)

        import torch

        generate_kwargs: dict[str, object] = {
            "max_new_tokens": self._max_new_tokens,
            "do_sample": False,
        }
        if self._compile_config is not None:
            generate_kwargs.update(
                {
                    "cache_implementation": "static",
                    "compile_config": self._compile_config,
                }
            )
        with torch.inference_mode():
            output_ids = model.generate(**inputs, **generate_kwargs)

        sequences = getattr(output_ids, "sequences", output_ids)
        generated_ids = sequences[:, inputs["input_ids"].shape[1] :]
        generated_tokens = int(generated_ids.shape[-1])
        truncated = (
            generated_tokens >= self._max_new_tokens
            and not _generation_ended_normally(model, generated_ids)
        )
        decoded = processor.decode(
            generated_ids,
            return_format="transcription_only",
            clean_up_tokenization_spaces=False,
        )
        if isinstance(decoded, str):
            return decoded.strip(), truncated
        text = decoded[0].strip() if decoded else ""
        return text, truncated

    def unload(self) -> None:
        """Release model and processor references."""
        self._model = None
        self._processor = None
        self._compile_config = None
