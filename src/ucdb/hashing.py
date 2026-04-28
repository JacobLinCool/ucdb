"""File hashing utilities for reproducibility."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

CHUNK = 1 << 20  # 1 MiB


def hash_file(path: Path | str, algorithm: str = "sha256") -> str:
    """Return ``algorithm:hexdigest`` for the file at *path*."""
    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return f"{algorithm}:{h.hexdigest()}"


def hash_text(text: str, algorithm: str = "sha256") -> str:
    """Return ``algorithm:hexdigest`` of *text* encoded as UTF-8."""
    h = hashlib.new(algorithm)
    h.update(text.encode("utf-8"))
    return f"{algorithm}:{h.hexdigest()}"


def file_size(path: Path | str) -> int:
    return Path(path).stat().st_size


def guess_mime(path: Path | str) -> str | None:
    mime, _ = mimetypes.guess_type(str(path))
    return mime
