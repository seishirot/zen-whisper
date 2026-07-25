"""Small, private TOML files written atomically for desktop settings."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomli_w


@dataclass(frozen=True)
class FileFingerprint:
    """Content and metadata identity used for optimistic file locking."""

    state: str
    size: int = 0
    mtime_ns: int = 0
    digest: str = ""


MISSING_FILE_FINGERPRINT = FileFingerprint("missing")


class StaleFileError(OSError):
    """Raised when a file changed after a settings snapshot was taken."""


def file_fingerprint(path: Path) -> FileFingerprint:
    """Return a stable best-effort identity without exposing file contents."""
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return MISSING_FILE_FINGERPRINT
    if not stat.S_ISREG(metadata.st_mode):
        return FileFingerprint(
            "other",
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
        )

    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        current = path.stat()
    except FileNotFoundError:
        return MISSING_FILE_FINGERPRINT
    return FileFingerprint(
        "file",
        size=current.st_size,
        mtime_ns=current.st_mtime_ns,
        digest=digest.hexdigest(),
    )


def atomic_write_toml(
    data: dict[str, object],
    path: Path,
    *,
    expected_fingerprint: FileFingerprint | None = None,
) -> None:
    """Write TOML beside its destination, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            tomli_w.dump(data, file)
            file.flush()
            os.fsync(file.fileno())
        if (
            expected_fingerprint is not None
            and file_fingerprint(path) != expected_fingerprint
        ):
            raise StaleFileError(
                f"{path.name} was changed outside ZenWhisper"
            )
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)
