"""Scan an input document repository.

Expected layout::

    <root>/<work-id>/<version-or-date>/<document.{pdf,docx,odt,txt,md}>
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .extract import SUPPORTED_EXTENSIONS


@dataclass(frozen=True)
class FoundDocument:
    work_id: str
    version_label: str
    path: Path

    @property
    def relative_key(self) -> str:
        return f"{self.work_id}/{self.version_label}/{self.path.name}"


def scan_repository(root: Path | str) -> Iterator[FoundDocument]:
    """Yield every supported document beneath *root*.

    Directories that don't follow the ``<work>/<version>/<file>`` convention
    are silently skipped so the scanner can be pointed at mixed trees.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"Input root is not a directory: {root_path}")

    for work_dir in sorted(p for p in root_path.iterdir() if p.is_dir()):
        for version_dir in sorted(p for p in work_dir.iterdir() if p.is_dir()):
            for entry in sorted(version_dir.iterdir()):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                yield FoundDocument(
                    work_id=work_dir.name,
                    version_label=version_dir.name,
                    path=entry,
                )
