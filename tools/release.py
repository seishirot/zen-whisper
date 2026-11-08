"""Prepare a release: move [Unreleased] notes to a version and bump metadata.

Usage (from the repository root):

    mise exec -- python -I tools/release.py prepare 1.2.0
    mise exec -- python -I tools/release.py check

``prepare`` rewrites CHANGELOG.md, sets ``[project].version`` in
pyproject.toml and refreshes the project's own entry in uv.lock with
``uv lock --offline``. It never commits, tags or pushes; it prints those
steps instead. ``check`` verifies that the three files agree.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
REPO_URL = "https://github.com/seishirot/zen-whisper"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", re.MULTILINE)


def parse(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"not a semantic version: {version}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def project_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def lock_version() -> str | None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return next((p["version"] for p in lock.get("package", []) if p.get("name") == "zen-whisper"), None)


def released_versions(text: str) -> list[str]:
    return [match.group(1) for match in RELEASE_HEADING.finditer(text)]


def unreleased_body(text: str) -> str:
    match = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' section")
    return match.group(1)


def problems() -> list[str]:
    text = CHANGELOG.read_text(encoding="utf-8").replace("\r\n", "\n")
    found = []
    try:
        unreleased_body(text)
    except ValueError as exc:
        found.append(str(exc))
    versions = released_versions(text)
    if not versions:
        found.append("CHANGELOG.md has no released version")
    elif versions[0] != project_version():
        found.append(f"latest CHANGELOG release {versions[0]} != pyproject version {project_version()}")
    if [parse(v) for v in versions] != sorted((parse(v) for v in versions), reverse=True) or len(set(versions)) != len(versions):
        found.append("CHANGELOG releases must be unique and newest first")
    if lock_version() != project_version():
        found.append(f"uv.lock zen-whisper version {lock_version()} != pyproject version {project_version()}")
    for version in versions:
        if f"[{version}]: {REPO_URL}/" not in text:
            found.append(f"CHANGELOG link for {version} is missing")
    return found


def prepare(version: str, date: str) -> int:
    if parse(version) <= parse(project_version()):
        raise SystemExit(f"{version} must be greater than the current {project_version()}")
    text = CHANGELOG.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = unreleased_body(text)
    if not body.strip():
        raise SystemExit("[Unreleased] is empty; describe the changes before releasing")
    previous = project_version()
    text = text.replace(f"## [Unreleased]\n{body}", f"## [Unreleased]\n\n## [{version}] - {date}\n{body}", 1)
    text = re.sub(r"^\[Unreleased\]: .*$", f"[Unreleased]: {REPO_URL}/compare/v{version}...HEAD\n"
                  f"[{version}]: {REPO_URL}/compare/v{previous}...v{version}", text, count=1, flags=re.MULTILINE)
    CHANGELOG.write_text(text, encoding="utf-8", newline="\n")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    pyproject, count = re.subn(r'^version = "[^"]+"$', f'version = "{version}"', pyproject, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit("could not find [project].version in pyproject.toml")
    PYPROJECT.write_text(pyproject, encoding="utf-8")
    subprocess.run(["uv", "lock", "--offline"], cwd=ROOT, check=True)
    remaining = problems()
    if remaining:
        print("\n".join(remaining), file=sys.stderr)
        return 1
    print(f"Prepared v{version}. Review, then (only when releasing is approved):\n"
          f"  git add CHANGELOG.md pyproject.toml uv.lock\n"
          f'  git commit -m "chore(release): v{version}"\n'
          f'  git tag -a v{version} -m "v{version}"\n'
          f"  git push origin main v{version}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    bump = commands.add_parser("prepare")
    bump.add_argument("version")
    bump.add_argument("--date", default=dt.date.today().isoformat())
    commands.add_parser("check")
    args = parser.parse_args()
    if args.command == "prepare":
        return prepare(args.version, args.date)
    found = problems()
    print("\n".join(found) if found else "release metadata ok", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
