"""Domain profile loading and replacement tests."""

from __future__ import annotations

import logging
from pathlib import Path

from src.profiles import Profile, ProfileTerm, apply_replacements, load_profiles


def test_load_profile_builds_recognition_hints(tmp_path):
    profile_path = tmp_path / "coding.toml"
    profile_path.write_text(
        """
name = "Coding"
context = "ZenWhisper の開発作業"

[[terms]]
canonical = "ZenWhisper"
spoken = ["ゼンウィスパー"]
replace_from = ["全ウィスパー"]
description = "製品名"
""",
        encoding="utf-8",
    )

    profiles = load_profiles(tmp_path)

    profile = profiles["coding"]
    assert profile.name == "Coding"
    assert profile.terms[0].replace_from == ("全ウィスパー",)
    hints = profile.recognition_hints()
    assert hints.hotwords == ("ZenWhisper", "ゼンウィスパー")
    assert "ZenWhisper の開発作業" in hints.context
    assert "製品名" in hints.context


def test_invalid_profile_does_not_hide_valid_profiles(tmp_path):
    (tmp_path / "valid.toml").write_text('name = "Valid"\n', encoding="utf-8")
    (tmp_path / "invalid.toml").write_text(
        """
name = "Invalid"
[[terms]]
canonical = "A"
replace_from = ["same"]
[[terms]]
canonical = "B"
replace_from = ["same"]
""",
        encoding="utf-8",
    )

    profiles = load_profiles(tmp_path)

    assert list(profiles) == ["valid"]


def test_invalid_profile_log_does_not_include_private_term_values(tmp_path, caplog):
    secret_source = "confidential-project-alias"
    secret_canonical_a = "ConfidentialProductA"
    secret_canonical_b = "ConfidentialProductB"
    (tmp_path / "private.toml").write_text(
        f"""
name = "Private"
[[terms]]
canonical = "{secret_canonical_a}"
replace_from = ["{secret_source}"]
[[terms]]
canonical = "{secret_canonical_b}"
replace_from = ["{secret_source}"]
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        profiles = load_profiles(tmp_path)

    assert profiles == {}
    assert secret_source not in caplog.text
    assert secret_canonical_a not in caplog.text
    assert secret_canonical_b not in caplog.text


def test_replacements_are_exact_longest_first_and_single_pass():
    profile = Profile(
        profile_id="test",
        name="Test",
        terms=(
            ProfileTerm(canonical="ZenWhisper", replace_from=("全ウィスパー", "全")),
            ProfileTerm(canonical="次", replace_from=("ZenWhisper",)),
        ),
    )

    result = apply_replacements("全ウィスパーと全とZenWhisper", profile)

    assert result == "ZenWhisperとZenWhisperと次"


def test_spoken_alias_is_not_an_implicit_replacement():
    profile = Profile(
        profile_id="test",
        name="Test",
        terms=(
            ProfileTerm(
                canonical="ZenWhisper",
                spoken=("ゼンウィスパー",),
                replace_from=("全ウィスパー",),
            ),
        ),
    )

    assert apply_replacements("ゼンウィスパー", profile) == "ゼンウィスパー"


def test_example_profile_corrects_observed_reazon_variants(tmp_path):
    example_path = (
        Path(__file__).resolve().parent.parent
        / "profiles"
        / "zen-whisper.toml.example"
    )
    (tmp_path / "zen-whisper.toml").write_text(
        example_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    profile = load_profiles(tmp_path)["zen-whisper"]

    result = apply_replacements(
        "全ウイスパー、オラアマ、リーズンK",
        profile,
    )

    assert result == "ZenWhisper、Ollama、Reazon K2"
