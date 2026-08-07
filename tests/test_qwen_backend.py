"""Transformers-native Qwen3-ASR backend tests."""

from __future__ import annotations

import contextlib
import sys
import types

import numpy as np
import pytest

from src.asr.base import RecognitionHints
from src.asr.qwen import (
    Qwen3Backend,
    _build_qwen3_prompt,
    _join_qwen3_transcripts,
    _prepare_qwen3_audio_chunks,
    _qwen3_chunk_sec,
    _split_qwen3_audio,
)
from src.config import (
    QWEN3_MODEL_LARGE,
    RecognitionConfig,
)


@pytest.fixture(autouse=True)
def _use_verified_test_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.asr.qwen.download_verified_snapshot",
        lambda _source: "/verified/qwen3-asr",
    )


class _FakeInputs(dict):
    def __init__(self) -> None:
        super().__init__(input_ids=np.asarray([[1, 2, 3]], dtype=np.int64))
        self.to_args: tuple[object, object] | None = None

    def to(self, device: object, dtype: object) -> _FakeInputs:
        self.to_args = (device, dtype)
        return self


class _FakeProcessor:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = list(outputs or [" result "])
        self.messages = None
        self.template_kwargs = None
        self.calls: list[dict[str, object]] = []
        self.decoded_ids: list[np.ndarray] = []
        self.decode_kwargs: list[dict[str, object]] = []
        self.inputs: list[_FakeInputs] = []

    def apply_chat_template(self, messages, **kwargs) -> str:
        self.messages = messages
        self.template_kwargs = kwargs
        return "PROMPT:"

    def __call__(self, **kwargs) -> _FakeInputs:
        self.calls.append(kwargs)
        inputs = _FakeInputs()
        self.inputs.append(inputs)
        return inputs

    def decode(self, generated_ids, **kwargs):
        self.decoded_ids.append(np.asarray(generated_ids))
        self.decode_kwargs.append(kwargs)
        return [self.outputs.pop(0)]


class _FakeModel:
    device = "cuda:0"
    dtype = "bfloat16"

    def __init__(self) -> None:
        self.generate_calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return np.asarray([[1, 2, 3, 9, 10]], dtype=np.int64)


def _install_fake_torch(monkeypatch) -> None:
    fake_torch = types.SimpleNamespace(
        inference_mode=lambda: contextlib.nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)


def test_prompt_preserves_profile_context_and_forced_language() -> None:
    processor = _FakeProcessor()

    prompt = _build_qwen3_prompt(
        processor,
        "project context",
        "Japanese",
    )

    assert prompt == "PROMPT:language Japanese<asr_text>"
    assert processor.messages == [
        {"role": "system", "content": "project context"},
        {"role": "user", "content": [{"type": "audio", "audio": ""}]},
    ]
    assert processor.template_kwargs == {
        "add_generation_prompt": True,
        "tokenize": False,
    }


def test_transcribe_generates_only_new_tokens_and_decodes_transcription(
    monkeypatch,
) -> None:
    _install_fake_torch(monkeypatch)
    processor = _FakeProcessor()
    model = _FakeModel()
    backend = Qwen3Backend()
    backend._model = model
    backend._processor = processor
    backend._max_new_tokens = 77

    text = backend.transcribe(
        np.zeros(1600, dtype=np.float32),
        "ja",
        RecognitionConfig(),
        RecognitionHints(context="project context", hotwords=("ZenWhisper",)),
    )

    assert text == "result"
    assert processor.calls[0]["text"] == [
        "PROMPT:language Japanese<asr_text>"
    ]
    assert len(processor.calls[0]["audio"][0]) == 8000
    assert processor.inputs[0].to_args == ("cuda:0", "bfloat16")
    assert model.generate_calls[0]["max_new_tokens"] == 77
    assert model.generate_calls[0]["do_sample"] is False
    np.testing.assert_array_equal(
        processor.decoded_ids[0],
        np.asarray([[9, 10]], dtype=np.int64),
    )
    assert processor.decode_kwargs[0] == {
        "return_format": "transcription_only",
        "clean_up_tokenization_spaces": False,
    }


def test_compile_configuration_is_forwarded_to_generation(monkeypatch) -> None:
    _install_fake_torch(monkeypatch)
    processor = _FakeProcessor()
    model = _FakeModel()
    compile_config = object()
    backend = Qwen3Backend()
    backend._model = model
    backend._processor = processor
    backend._compile_config = compile_config

    backend.transcribe(
        np.zeros(8000, dtype=np.float32),
        "en",
        RecognitionConfig(),
    )

    assert model.generate_calls[0]["cache_implementation"] == "static"
    assert model.generate_calls[0]["compile_config"] is compile_config
    assert processor.calls[0]["text"] == [
        "PROMPT:language English<asr_text>"
    ]


def test_long_audio_chunks_reconstruct_original_without_gaps() -> None:
    audio = np.linspace(-1.0, 1.0, 65_537, dtype=np.float32)

    chunks = _split_qwen3_audio(audio, max_chunk_sec=1.5)

    assert len(chunks) > 1
    assert all(len(chunk) > 0 for chunk in chunks)
    assert all(len(chunk) <= 24_000 for chunk in chunks)
    np.testing.assert_array_equal(np.concatenate(chunks), audio)


def test_chunk_splitter_prefers_quiet_boundary_before_maximum() -> None:
    audio = np.ones(40_000, dtype=np.float32)
    audio[13_500:15_500] = 0

    chunks = _split_qwen3_audio(audio, max_chunk_sec=1.0)

    assert len(chunks[0]) == 13_500
    assert all(len(chunk) <= 16_000 for chunk in chunks)
    np.testing.assert_array_equal(np.concatenate(chunks), audio)


def test_chunk_duration_scales_with_output_token_budget() -> None:
    assert _qwen3_chunk_sec(256) == 60.0
    assert _qwen3_chunk_sec(128) == 32.0
    assert _qwen3_chunk_sec(1) == 1.0


def test_prepared_chunks_overlap_without_exceeding_maximum() -> None:
    audio = np.arange(130 * 16_000, dtype=np.float32)

    chunks = _prepare_qwen3_audio_chunks(audio, max_chunk_sec=60.0)

    assert len(chunks) > 1
    assert all(len(chunk) <= 60 * 16_000 for chunk in chunks)
    reconstructed = [chunks[0]]
    for left, right in zip(chunks[:-1], chunks[1:], strict=True):
        shared_start = int(np.searchsorted(left, right[0]))
        assert left[shared_start] == right[0]
        shared_samples = len(left) - shared_start
        assert 0 < shared_samples <= 16_000
        reconstructed.append(right[shared_samples:])
    np.testing.assert_array_equal(np.concatenate(reconstructed), audio)


def test_overlapping_transcripts_are_aligned_before_joining() -> None:
    assert _join_qwen3_transcripts(
        ["an international", "international event"],
        "English",
    ) == "an international event"
    assert _join_qwen3_transcripts(
        ["これは前半後半", "前半後半です"],
        "Japanese",
    ) == "これは前半後半です"
    assert _join_qwen3_transcripts(
        ["in the", "theory matters"],
        "English",
    ) == "in the theory matters"


def test_english_contractions_and_quotes_are_joined_with_context() -> None:
    assert _join_qwen3_transcripts(["don", "'t stop"], "English") == "don't stop"
    assert _join_qwen3_transcripts(["hello", '"'], "English") == 'hello"'
    assert _join_qwen3_transcripts(["He said", '"hello"'], "English") == (
        'He said "hello"'
    )
    assert _join_qwen3_transcripts(["He said", "'hello'"], "English") == (
        "He said 'hello'"
    )
    assert _join_qwen3_transcripts(['"hello"', "world"], "English") == (
        '"hello" world'
    )
    assert _join_qwen3_transcripts(["James'", "book"], "English") == (
        "James' book"
    )
    assert _join_qwen3_transcripts(
        ["He said 'hello", "' and left"],
        "English",
    ) == "He said 'hello' and left"
    assert _join_qwen3_transcripts(
        ["He said", '"', "hello", '"'],
        "English",
    ) == 'He said "hello"'
    assert _join_qwen3_transcripts(
        ["He said", "'", "hello", "'"],
        "English",
    ) == "He said 'hello'"
    assert _join_qwen3_transcripts(
        ["He said", "“", "hello", "”"],
        "English",
    ) == "He said “hello”"
    assert _join_qwen3_transcripts(
        ["He said", "'\"", "hello", "\"'"],
        "English",
    ) == "He said '\"hello\"'"


def test_empty_audio_returns_without_generation() -> None:
    processor = _FakeProcessor()
    model = _FakeModel()
    backend = Qwen3Backend()
    backend._model = model
    backend._processor = processor

    assert (
        backend.transcribe(
            np.asarray([], dtype=np.float32),
            "ja",
            RecognitionConfig(),
        )
        == ""
    )
    assert processor.calls == []
    assert model.generate_calls == []


def test_transcribe_concatenates_long_audio_results(monkeypatch) -> None:
    _install_fake_torch(monkeypatch)
    processor = _FakeProcessor(outputs=["first", "second"])
    model = _FakeModel()
    backend = Qwen3Backend()
    backend._model = model
    backend._processor = processor
    audio = np.zeros(24_001, dtype=np.float32)
    monkeypatch.setattr(
        "src.asr.qwen._split_qwen3_audio",
        lambda value, max_chunk_sec: [value[:12_000], value[12_000:]],
    )

    text = backend.transcribe(audio, "en", RecognitionConfig())

    assert text == "first second"
    assert len(model.generate_calls) == 2


def test_japanese_chunk_results_align_overlapping_text(monkeypatch) -> None:
    _install_fake_torch(monkeypatch)
    processor = _FakeProcessor(outputs=["これは前半後半", "前半後半です"])
    backend = Qwen3Backend()
    backend._model = _FakeModel()
    backend._processor = processor
    audio = np.zeros(24_001, dtype=np.float32)
    monkeypatch.setattr(
        "src.asr.qwen._split_qwen3_audio",
        lambda value, max_chunk_sec: [value[:12_000], value[12_000:]],
    )

    assert (
        backend.transcribe(audio, "ja", RecognitionConfig())
        == "これは前半後半です"
    )


def test_token_limit_retries_smaller_overlapping_chunks(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_fake_torch(monkeypatch)

    class RetryModel(_FakeModel):
        def generate(self, **kwargs):
            self.generate_calls.append(kwargs)
            if len(self.generate_calls) == 1:
                return np.asarray(
                    [[1, 2, 3, 9, 10, 11, 12, 13, 14, 15, 16]],
                    dtype=np.int64,
                )
            return np.asarray([[1, 2, 3, 9]], dtype=np.int64)

    backend = Qwen3Backend()
    model = RetryModel()
    backend._model = model
    backend._processor = _FakeProcessor(outputs=["discard"] + ["part"] * 12)
    backend._max_new_tokens = 8

    with caplog.at_level("WARNING", logger="src.asr.qwen"):
        text = backend.transcribe(
            np.zeros(32_000, dtype=np.float32),
            "ja",
            RecognitionConfig(),
        )

    assert text == "part"
    assert len(model.generate_calls) > 1
    assert "再分割します" in caplog.text


def test_token_limit_at_minimum_chunk_fails_instead_of_returning_partial(
    monkeypatch,
) -> None:
    _install_fake_torch(monkeypatch)
    backend = Qwen3Backend()
    backend._model = _FakeModel()
    backend._processor = _FakeProcessor()
    backend._max_new_tokens = 2

    with pytest.raises(RuntimeError, match="生成上限"):
        backend.transcribe(
            np.zeros(8000, dtype=np.float32),
            "ja",
            RecognitionConfig(),
        )


def test_repeated_token_limit_recursion_terminates_with_failure(
    monkeypatch,
) -> None:
    _install_fake_torch(monkeypatch)

    class AlwaysTruncatedModel(_FakeModel):
        def generate(self, **kwargs):
            self.generate_calls.append(kwargs)
            return np.asarray(
                [[1, 2, 3, 9, 10, 11, 12, 13, 14, 15, 16]],
                dtype=np.int64,
            )

    backend = Qwen3Backend()
    model = AlwaysTruncatedModel()
    backend._model = model
    backend._processor = _FakeProcessor(outputs=["partial"] * 32)
    backend._max_new_tokens = 8

    with pytest.raises(RuntimeError, match="生成上限"):
        backend._transcribe_chunk_with_retry(
            np.zeros(8 * 16_000, dtype=np.float32),
            "",
            "Japanese",
        )

    assert len(model.generate_calls) >= 3


def test_successful_retry_sibling_is_not_returned_if_later_sibling_exhausts(
    monkeypatch,
) -> None:
    _install_fake_torch(monkeypatch)

    class FirstSiblingThenFailModel(_FakeModel):
        def generate(self, **kwargs):
            self.generate_calls.append(kwargs)
            if len(self.generate_calls) == 2:
                return np.asarray([[1, 2, 3, 9]], dtype=np.int64)
            return np.asarray(
                [[1, 2, 3, 9, 10, 11, 12, 13, 14, 15, 16]],
                dtype=np.int64,
            )

    backend = Qwen3Backend()
    model = FirstSiblingThenFailModel()
    backend._model = model
    backend._processor = _FakeProcessor(outputs=["partial"] * 16)
    backend._max_new_tokens = 8

    with pytest.raises(RuntimeError, match="生成上限"):
        backend._transcribe_chunk_with_retry(
            np.zeros(2 * 16_000, dtype=np.float32),
            "",
            "Japanese",
        )

    assert len(model.generate_calls) >= 3


def test_token_limit_with_eos_does_not_retry(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_fake_torch(monkeypatch)
    model = _FakeModel()
    model.generation_config = types.SimpleNamespace(eos_token_id=10)
    backend = Qwen3Backend()
    backend._model = model
    backend._processor = _FakeProcessor()
    backend._max_new_tokens = 2

    with caplog.at_level("WARNING", logger="src.asr.qwen"):
        text = backend.transcribe(
            np.zeros(8000, dtype=np.float32),
            "ja",
            RecognitionConfig(),
        )

    assert text == "result"
    assert len(model.generate_calls) == 1
    assert "再分割します" not in caplog.text


def test_load_uses_native_auto_classes_and_wires_compile_to_generation(
    monkeypatch,
) -> None:
    processor = _FakeProcessor()

    class ProcessorFactory:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            cls.calls.append((args, kwargs))
            return processor

    class LoadedModel(_FakeModel):
        def __init__(self) -> None:
            super().__init__()
            self.to_calls: list[str] = []
            self.eval_calls = 0

        def to(self, device: str):
            self.to_calls.append(device)
            return self

        def eval(self):
            self.eval_calls += 1
            return self

    loaded_model = LoadedModel()

    class ModelFactory:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            cls.calls.append((args, kwargs))
            return loaded_model

    class CompileConfig:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    fake_transformers = types.SimpleNamespace(
        AutoModelForMultimodalLM=ModelFactory,
        AutoProcessor=ProcessorFactory,
        CompileConfig=CompileConfig,
    )
    fake_torch = types.SimpleNamespace(
        bfloat16="bf16",
        inference_mode=lambda: contextlib.nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr("src.asr.qwen._is_triton_available", lambda: True)

    backend = Qwen3Backend()
    cfg = RecognitionConfig(
        qwen3_model=QWEN3_MODEL_LARGE,
        qwen3_max_new_tokens=321,
        qwen3_attn_implementation="sdpa",
        qwen3_torch_compile=True,
        device="cuda",
        model_load_timeout_sec=1,
    )

    backend.load(cfg)

    assert ProcessorFactory.calls == [
        (
            ("/verified/qwen3-asr",),
            {"local_files_only": True, "trust_remote_code": False},
        )
    ]
    assert ModelFactory.calls == [
        (
            ("/verified/qwen3-asr",),
            {
                "dtype": "bf16",
                "attn_implementation": "sdpa",
                "local_files_only": True,
                "trust_remote_code": False,
            },
        )
    ]
    assert loaded_model.to_calls == ["cuda"]
    assert loaded_model.eval_calls == 1
    assert backend._model is loaded_model
    assert backend._processor is processor
    assert backend._max_new_tokens == 321
    assert isinstance(backend._compile_config, CompileConfig)
    assert backend._compile_config.kwargs == {"mode": "reduce-overhead"}
    assert backend.is_ready is True

    backend.transcribe(
        np.zeros(8000, dtype=np.float32),
        "en",
        cfg,
    )

    assert loaded_model.generate_calls[0]["cache_implementation"] == "static"
    assert (
        loaded_model.generate_calls[0]["compile_config"]
        is backend._compile_config
    )


@pytest.mark.parametrize("value", [0.5, True, 0, -1])
def test_load_rejects_non_positive_integer_token_limit(value) -> None:
    backend = Qwen3Backend()

    with pytest.raises(ValueError, match="正の整数"):
        backend.load(RecognitionConfig(qwen3_max_new_tokens=value))

    assert backend.is_ready is False


def test_legacy_model_id_is_rejected_before_loading() -> None:
    backend = Qwen3Backend()

    with pytest.raises(ValueError, match=r"-hf"):
        backend.load(
            RecognitionConfig(qwen3_model="Qwen/Qwen3-ASR-1.7B"),
        )

    assert backend.is_ready is False


def test_failed_load_does_not_keep_partial_processor(monkeypatch) -> None:
    class ProcessorFactory:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return object()

    class ModelFactory:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            raise RuntimeError("model load failed")

    fake_transformers = types.SimpleNamespace(
        AutoModelForMultimodalLM=ModelFactory,
        AutoProcessor=ProcessorFactory,
        CompileConfig=object,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(bfloat16="bf16"),
    )
    backend = Qwen3Backend()

    with pytest.raises(RuntimeError, match="model load failed"):
        backend.load(
            RecognitionConfig(
                qwen3_model=QWEN3_MODEL_LARGE,
                qwen3_attn_implementation="sdpa",
                device="cuda",
                model_load_timeout_sec=1,
            )
        )

    assert backend._model is None
    assert backend._processor is None
    assert backend.is_ready is False


def test_model_without_processor_is_not_ready() -> None:
    backend = Qwen3Backend()
    backend._model = object()

    assert backend.is_ready is False


def test_unload_clears_all_runtime_references() -> None:
    backend = Qwen3Backend()
    backend._model = object()
    backend._processor = object()
    backend._compile_config = object()

    backend.unload()

    assert backend._model is None
    assert backend._processor is None
    assert backend._compile_config is None
    assert backend.is_ready is False
