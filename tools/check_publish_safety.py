"""Pre-push privacy gate for outgoing commits (stdlib only).

Scans the commits that a push would publish and fails when they contain:

- paths that must stay local (evaluation outputs, models, binaries, configs,
  logs, recordings outside the synthetic fixtures);
- text copied from local private references: any 12-character normalized,
  mostly non-ASCII window of a reference in tools/bench_outputs/private/**/manifest.json
  that appears in added lines or commit messages;
- local identity details: e-mail addresses other than allowed no-reply
  addresses, user-profile paths, this machine's host name and user name,
  NVIDIA GPU UUIDs, and entries from an optional local deny list
  (tools/bench_outputs/private/publish-denylist.local.txt, one per line);
- tracked files that are now ignored.

Matches are reported by category and location only; matched private text is
never printed. A line containing "publish-safety: allow" is exempt; use it
only for synthetic test data. Run ``python -I tools/check_publish_safety.py install-hook``
once per clone to enable the pre-push hook, which also runs gitleaks.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / "tools/bench_outputs/private"
DENYLIST = PRIVATE_ROOT / "publish-denylist.local.txt"
HOOK_SOURCE = ROOT / "tools/hooks/pre-push"
WINDOW = 12
ZERO_SHA = "0" * 40
# A line containing this marker is exempt (for synthetic test data only).
ALLOW_MARKER = "publish-safety: allow"

FORBIDDEN_PATHS = (
    "tools/bench_outputs/*", "tools/models/*", "tools/bin/*", "tools/downloads/*",
    "config.toml", "postprocessors.toml", "*.log", "*.log.*", "*.local.*",
    "*.m4a", "*.mp3", "*.wav", "*.flac", "*.ogg", "*.opus", "*.gguf", "*.nemo",
    "*.onnx.data", "*.safetensors", "*.bin",
)
ALLOWED_PATHS = ("tools/samples/*.wav", "assets/*.flac", "assets/*.wav", "assets/*.mp3")
ALLOWED_EMAILS = re.compile(
    r"(noreply@anthropic\.com|[\w.+-]+@users\.noreply\.github\.com|[\w.+-]+@example\.(com|org|net))$",
    re.IGNORECASE,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.IGNORECASE)
USER_PATH = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)([^\\/\s<>%$\"'`{}()|]+)")
PLACEHOLDER_USERS = {"user", "username", "name", "me", "runner", "you", "example", "<user>", "public", "default", "shared"}
GPU_UUID = re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    category: str
    commit: str
    location: str

    def render(self) -> str:
        return f"[{self.category}] {self.commit[:10]} {self.location}"


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("utf-8", "replace")


def squash(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in value if unicodedata.category(c)[0] not in ("P", "Z", "S", "C"))


def private_windows(root: Path = PRIVATE_ROOT) -> set[str]:
    """Collect normalized 12-character windows of local private references."""
    windows: set[str] = set()
    if not root.exists():
        return windows
    for manifest in root.rglob("manifest.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in data.get("items", []) if isinstance(data, dict) else []:
            reference = squash(str(item.get("reference", "")))
            windows.update(
                window for window in (reference[i:i + WINDOW] for i in range(max(0, len(reference) - WINDOW + 1)))
                # Shared technical identifiers (model names, file names) are not private text.
                if sum(not char.isascii() for char in window) >= WINDOW // 2
            )
    return windows


def deny_terms(path: Path = DENYLIST) -> list[str]:
    terms = [platform.node()]
    try:
        terms.append(getpass.getuser())
    except Exception:
        pass
    if path.exists():
        terms += [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    return sorted({term for term in terms if len(term) >= 3}, key=str.casefold)


def path_forbidden(path: str) -> bool:
    if any(fnmatchcase(path, pattern) for pattern in ALLOWED_PATHS):
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path, pattern) or fnmatchcase(name, pattern) for pattern in FORBIDDEN_PATHS)


def scan_text(text: str, *, windows: set[str], terms: list[str]) -> list[str]:
    """Return finding categories for one piece of outgoing text."""
    categories: list[str] = []
    if ALLOW_MARKER in text:
        return categories
    for email in EMAIL.findall(text):
        if not ALLOWED_EMAILS.fullmatch(email):
            categories.append("email")
    for user in USER_PATH.findall(text):
        if user.casefold() not in PLACEHOLDER_USERS and not user.startswith(("<", "$", "%", "{")):
            categories.append("user-path")
    if any(not match.group().casefold().startswith("gpu-00000000-0000-0000-0000-") for match in GPU_UUID.finditer(text)):
        categories.append("gpu-uuid")  # all-zero prefixes are test placeholders
    folded = text.casefold()
    for term in terms:
        if re.search(rf"(?<![\w]){re.escape(term.casefold())}(?![\w])", folded):
            categories.append("deny-list")
    if windows:
        flat = squash(text)
        if any(flat[i:i + WINDOW] in windows for i in range(max(0, len(flat) - WINDOW + 1))):
            categories.append("private-reference")
    return categories


def outgoing_commits(revision_range: str) -> list[str]:
    output = git("rev-list", "--reverse", *revision_range.split())
    return [line for line in output.splitlines() if line]


def scan_commit(commit: str, *, windows: set[str], terms: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    message = git("log", "-1", "--format=%B", commit)
    for category in scan_text(message, windows=windows, terms=terms):
        findings.append(Finding(category, commit, "commit message"))
    changes = git("diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-M", commit)
    for line in changes.splitlines():
        parts = line.split("\t")
        if parts and parts[0][:1] != "D" and path_forbidden(parts[-1]):
            findings.append(Finding("forbidden-path", commit, parts[-1]))
    patch = git("show", "--format=", "--unified=0", "--no-color", "--text", commit)
    current, number = "?", 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            number = int(match.group(1)) if match else 0
        elif line.startswith("+"):
            for category in scan_text(line[1:], windows=windows, terms=terms):
                findings.append(Finding(category, commit, f"{current}:{number}"))
            number += 1
    return findings


def ignored_tracked_files() -> list[str]:
    output = git("ls-files", "--cached", "--ignored", "--exclude-standard")
    return [line for line in output.splitlines() if line]


def ranges_from_pre_push(stdin_text: str) -> list[str]:
    ranges = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA:
            continue  # deletion
        local_sha, remote_sha = parts[1], parts[3]
        ranges.append(f"{local_sha} --not --remotes" if remote_sha == ZERO_SHA else f"{remote_sha}..{local_sha}")
    return ranges


def check(ranges: list[str]) -> int:
    windows, terms = private_windows(), deny_terms()
    findings: list[Finding] = []
    commits: list[str] = []
    for revision_range in ranges:
        commits += [c for c in outgoing_commits(revision_range) if c not in commits]
    for commit in commits:
        findings += scan_commit(commit, windows=windows, terms=terms)
    findings += [Finding("ignored-but-tracked", "HEAD", path) for path in ignored_tracked_files()]
    print(f"publish-safety: {len(commits)} commit(s), {len(windows)} private reference windows, "
          f"{len(terms)} deny terms", file=sys.stderr)
    for finding in dict.fromkeys(findings):
        print(f"publish-safety: {finding.render()}", file=sys.stderr)
    if findings:
        print("publish-safety: blocked. Remove the data from the outgoing commits "
              "(rewrite unpublished history), or adjust the local deny list if this is a false positive.",
              file=sys.stderr)
        return 1
    print("publish-safety: ok", file=sys.stderr)
    return 0


def install_hook() -> int:
    hooks = Path(git("rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else ROOT / hooks
    target = hooks / "pre-push"
    source = HOOK_SOURCE.read_bytes()
    if target.exists() and target.read_bytes() != source:
        print(f"{target} already exists with other contents; not overwritten", file=sys.stderr)
        return 1
    hooks.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    os.chmod(target, 0o755)
    print(f"installed {target}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="scan a revision range (default: @{upstream}..HEAD)")
    run.add_argument("range", nargs="?", default="@{upstream}..HEAD")
    commands.add_parser("pre-push", help="read git pre-push stdin and scan the pushed ranges")
    commands.add_parser("install-hook", help="install tools/hooks/pre-push into this clone")
    args = parser.parse_args()
    if args.command == "install-hook":
        return install_hook()
    if args.command == "pre-push":
        return check(ranges_from_pre_push(sys.stdin.read()))
    return check([args.range])


if __name__ == "__main__":
    raise SystemExit(main())
