"""Domain profile loading and deterministic transcript corrections."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.asr.base import RecognitionHints

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent
_PROFILES_DIR = _ROOT_DIR / "profiles"


class ProfileError(ValueError):
    """Raised when a profile file is invalid."""


@dataclass(frozen=True)
class ProfileTerm:
    """A canonical term and the variants used by ASR/postprocessing."""

    canonical: str
    spoken: tuple[str, ...] = ()
    replace_from: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Profile:
    """Knowledge shared by recognition hints and transcript postprocessing."""

    profile_id: str
    name: str
    context: str = ""
    terms: tuple[ProfileTerm, ...] = ()

    def recognition_hints(self) -> RecognitionHints:
        """Build backend-neutral ASR hints from this profile."""
        hotwords: list[str] = []
        for term in self.terms:
            hotwords.append(term.canonical)
            hotwords.extend(term.spoken)

        context_parts: list[str] = []
        if self.context.strip():
            context_parts.append(self.context.strip())
        if self.terms:
            context_parts.append("重要語彙:\n" + render_terms(self))

        return RecognitionHints(
            context="\n\n".join(context_parts),
            hotwords=tuple(dict.fromkeys(word for word in hotwords if word)),
        )


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{field_name} は文字列の配列で指定してください")
    return tuple(item.strip() for item in value if item.strip())


def _parse_profile(path: Path) -> Profile:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except Exception as exc:
        raise ProfileError(f"TOML を読み込めません: {type(exc).__name__}") from exc

    name = data.get("name", path.stem)
    context = data.get("context", "")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("name は空でない文字列で指定してください")
    if not isinstance(context, str):
        raise ProfileError("context は文字列で指定してください")

    raw_terms = data.get("terms", [])
    if not isinstance(raw_terms, list):
        raise ProfileError("terms は [[terms]] の配列で指定してください")

    terms: list[ProfileTerm] = []
    replacements: dict[str, tuple[str, int]] = {}
    for index, raw_term in enumerate(raw_terms):
        if not isinstance(raw_term, dict):
            raise ProfileError(f"terms[{index}] はテーブルで指定してください")
        canonical = raw_term.get("canonical", "")
        description = raw_term.get("description", "")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ProfileError(f"terms[{index}].canonical は空でない文字列で指定してください")
        if not isinstance(description, str):
            raise ProfileError(f"terms[{index}].description は文字列で指定してください")

        canonical = canonical.strip()
        replace_from = _string_list(
            raw_term.get("replace_from"),
            f"terms[{index}].replace_from",
        )
        for source_index, source in enumerate(replace_from):
            previous = replacements.get(source)
            if previous is not None and previous[0] != canonical:
                raise ProfileError(
                    f"terms[{index}].replace_from[{source_index}] が "
                    f"terms[{previous[1]}] の置換元と重複しています"
                )
            replacements[source] = (canonical, index)

        terms.append(
            ProfileTerm(
                canonical=canonical,
                spoken=_string_list(raw_term.get("spoken"), f"terms[{index}].spoken"),
                replace_from=replace_from,
                description=description.strip(),
            )
        )

    return Profile(
        profile_id=path.stem,
        name=name.strip(),
        context=context.strip(),
        terms=tuple(terms),
    )


def load_profiles(directory: Path | None = None) -> dict[str, Profile]:
    """Load every ``*.toml`` profile in a directory.

    Invalid files are skipped independently so one typo does not remove the
    remaining profiles from the tray menu.
    """
    profile_dir = directory or _PROFILES_DIR
    if not profile_dir.is_dir():
        logger.info("プロファイルディレクトリが見つかりません: %s", profile_dir)
        return {}

    profiles: dict[str, Profile] = {}
    for path in sorted(profile_dir.glob("*.toml")):
        try:
            profile = _parse_profile(path)
        except ProfileError as exc:
            logger.warning("プロファイルを読み込めません: file=%s reason=%s", path.name, exc)
            continue
        profiles[profile.profile_id] = profile

    logger.info("プロファイルを読み込みました: count=%d", len(profiles))
    return profiles


def apply_replacements(text: str, profile: Profile | None) -> str:
    """Apply explicit ``replace_from`` mappings once, longest match first."""
    if profile is None:
        return text

    replacements: dict[str, str] = {}
    for term in profile.terms:
        for source in term.replace_from:
            replacements[source] = term.canonical
    if not replacements:
        return text

    pattern = re.compile(
        "|".join(re.escape(source) for source in sorted(replacements, key=len, reverse=True))
    )
    return pattern.sub(lambda match: replacements[match.group(0)], text)


def render_terms(profile: Profile | None) -> str:
    """Render terms as compact context for an LLM prompt."""
    if profile is None or not profile.terms:
        return "（なし）"

    lines: list[str] = []
    for term in profile.terms:
        details: list[str] = []
        if term.spoken:
            details.append(f"読み・呼び方: {', '.join(term.spoken)}")
        if term.replace_from:
            details.append(f"よくある誤認識: {', '.join(term.replace_from)}")
        if term.description:
            details.append(term.description)
        suffix = f" — {'; '.join(details)}" if details else ""
        lines.append(f"- {term.canonical}{suffix}")
    return "\n".join(lines)
