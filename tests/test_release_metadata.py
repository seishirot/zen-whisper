"""Version, changelog and lockfile must agree; release prep is mechanical."""
import subprocess

import pytest

from tools import release


def test_repository_release_metadata_is_consistent():
    assert release.problems() == []


def test_prepare_moves_unreleased_notes_and_bumps_versions(monkeypatch, tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- New thing.\n\n"
        "## [1.1.0] - 2026-01-01\n\n- Old.\n\n"
        f"[Unreleased]: {release.REPO_URL}/compare/v1.1.0...HEAD\n"
        f"[1.1.0]: {release.REPO_URL}/compare/v1.0.0...v1.1.0\n",
        encoding="utf-8",
    )
    pyproject.write_text('[project]\nname = "zen-whisper"\nversion = "1.1.0"\n', encoding="utf-8")
    lock.write_text('[[package]]\nname = "zen-whisper"\nversion = "1.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    monkeypatch.setattr(release, "LOCK", lock)

    def fake_lock(*args, **kwargs):
        lock.write_text('[[package]]\nname = "zen-whisper"\nversion = "1.2.0"\n', encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(release.subprocess, "run", fake_lock)

    assert release.prepare("1.2.0", "2026-02-02") == 0
    text = changelog.read_text(encoding="utf-8")
    assert "## [Unreleased]\n\n## [1.2.0] - 2026-02-02\n\n### Added\n\n- New thing." in text
    assert f"[1.2.0]: {release.REPO_URL}/compare/v1.1.0...v1.2.0" in text
    assert 'version = "1.2.0"' in pyproject.read_text(encoding="utf-8")
    assert release.problems() == []


def test_prepare_refuses_empty_unreleased_or_lower_version(monkeypatch, tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    pyproject = tmp_path / "pyproject.toml"
    changelog.write_text("## [Unreleased]\n\n## [1.1.0] - 2026-01-01\n", encoding="utf-8")
    pyproject.write_text('[project]\nversion = "1.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    monkeypatch.setattr(release, "PYPROJECT", pyproject)
    with pytest.raises(SystemExit, match="empty"):
        release.prepare("1.2.0", "2026-02-02")
    with pytest.raises(SystemExit, match="greater"):
        release.prepare("1.0.9", "2026-02-02")
