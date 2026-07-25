"""Pure helper tests for the Reazon dynamic-hotword benchmark."""

from __future__ import annotations

import pytest

from tools.bench_reazon_hotwords import (
    encode_dynamic_hotwords,
    model_filenames,
)


def test_model_filenames_select_int8_encoder_decoder_and_joiner() -> None:
    assert model_filenames("ja", "int8") == {
        "tokens": "tokens.txt",
        "encoder": "encoder-epoch-99-avg-1.int8.onnx",
        "decoder": "decoder-epoch-99-avg-1.int8.onnx",
        "joiner": "joiner-epoch-99-avg-1.int8.onnx",
    }


def test_encode_dynamic_hotwords_tokenizes_each_character_and_deduplicates() -> None:
    symbols = set("Zen音声")

    encoded = encode_dynamic_hotwords(["Zen", "音声", "Zen"], symbols)

    assert encoded == "Z e n/音 声"


def test_encode_dynamic_hotwords_rejects_unknown_tokens() -> None:
    with pytest.raises(ValueError, match="unknown token"):
        encode_dynamic_hotwords(["Zen"], set("Ze"))


def test_encode_dynamic_hotwords_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        encode_dynamic_hotwords(["Zen Whisper"], set("ZenWhisper"))
