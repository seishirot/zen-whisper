"""Logging setup for the native macOS backend."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class PrivateRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:  # noqa: N802 - stdlib override name
        super().doRollover()
        for path in Path(self.baseFilename).parent.glob(Path(self.baseFilename).name + "*"):
            if path.is_file():
                os.chmod(path, 0o600)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    log_file = log_dir / "backend.log"
    handler = PrivateRotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    if log_file.exists():
        os.chmod(log_file, 0o600)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
