"""Compute revisions (version-to-version diffs) and persist them.

A *revision* compares two ``document_versions`` rows of the same code. Sections
are matched across versions by their USLM ``identifier`` attribute — the only
key the schema guarantees to be stable across revisions. Sections without an
identifier are reported as anonymous additions/removals because we have no
reliable way to align them.

The :func:`compare_section_sets` function is the pure comparison core, used
both by :func:`compute_revision` (which persists results into ``revisions`` /
``section_changes``) and by the on-the-fly :func:`db.diff_versions` query that
serves arbitrary version pairs.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from dataclasses import dataclass

from . import db


@dataclass
class SectionChange:
    """A single section-level change between two versions."""

    change_type: str  # "added" | "removed" | "modified"
    identifier: str | None
    level: str
    num: str | None
    heading: str | None
    from_section_id: int | None
    to_section_id: int | None
    text_diff: str | None  # unified diff for "modified", else None


@dataclass
class CompareStats:
    added: int
    removed: int
    modified: int
    unchanged: int


@dataclass
class RevisionStats:
    revision_id: int
    added: int
    removed: int
    modified: int
    unchanged: int


def _index_by_identifier(
    rows: list[sqlite3.Row],
) -> tuple[dict[str, sqlite3.Row], list[sqlite3.Row]]:
    """Split *rows* into (identified_by_id, anonymous)."""
    keyed: dict[str, sqlite3.Row] = {}
    anonymous: list[sqlite3.Row] = []
    for row in rows:
        ident = row["identifier"]
        if ident:
            keyed[ident] = row
        else:
            anonymous.append(row)
    return keyed, anonymous


def text_diff(before: str | None, after: str | None) -> str:
    """Render a unified diff between two section bodies. Public for reuse."""
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    if not a and not b:
        return ""
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n"
    return "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after", n=2))


def compare_section_sets(
    from_sections: list[sqlite3.Row],
    to_sections: list[sqlite3.Row],
) -> tuple[list[SectionChange], CompareStats]:
    """Compare two sets of section rows by USLM identifier.

    Sections without an identifier are reported as anonymous additions/removals
    (they cannot be aligned across versions). Headings and content are both
    compared; a difference in either marks the section as ``modified``.
    """
    to_by_id, to_anon = _index_by_identifier(to_sections)
    from_by_id, from_anon = _index_by_identifier(from_sections)

    changes: list[SectionChange] = []
    added = removed = modified = unchanged = 0

    for ident, to_row in to_by_id.items():
        from_row = from_by_id.get(ident)
        if from_row is None:
            added += 1
            changes.append(
                SectionChange(
                    change_type="added",
                    identifier=ident,
                    level=to_row["level"],
                    num=to_row["num"],
                    heading=to_row["heading"],
                    from_section_id=None,
                    to_section_id=to_row["id"],
                    text_diff=None,
                )
            )
            continue

        same_content = (from_row["content"] or "") == (to_row["content"] or "")
        same_heading = (from_row["heading"] or "") == (to_row["heading"] or "")
        if same_content and same_heading:
            unchanged += 1
            continue

        modified += 1
        changes.append(
            SectionChange(
                change_type="modified",
                identifier=ident,
                level=to_row["level"],
                num=to_row["num"],
                heading=to_row["heading"],
                from_section_id=from_row["id"],
                to_section_id=to_row["id"],
                text_diff=text_diff(from_row["content"], to_row["content"]),
            )
        )

    for ident, from_row in from_by_id.items():
        if ident in to_by_id:
            continue
        removed += 1
        changes.append(
            SectionChange(
                change_type="removed",
                identifier=ident,
                level=from_row["level"],
                num=from_row["num"],
                heading=from_row["heading"],
                from_section_id=from_row["id"],
                to_section_id=None,
                text_diff=None,
            )
        )

    for row in to_anon:
        added += 1
        changes.append(
            SectionChange(
                change_type="added",
                identifier=None,
                level=row["level"],
                num=row["num"],
                heading=row["heading"],
                from_section_id=None,
                to_section_id=row["id"],
                text_diff=None,
            )
        )
    for row in from_anon:
        removed += 1
        changes.append(
            SectionChange(
                change_type="removed",
                identifier=None,
                level=row["level"],
                num=row["num"],
                heading=row["heading"],
                from_section_id=row["id"],
                to_section_id=None,
                text_diff=None,
            )
        )

    return changes, CompareStats(
        added=added, removed=removed, modified=modified, unchanged=unchanged
    )


def compute_revision(
    conn: sqlite3.Connection,
    *,
    code_id: str,
    to_version_id: int,
    from_version_id: int | None,
) -> RevisionStats:
    """Compute and persist a revision between two versions.

    If a revision row already exists for the (from, to) pair, it is replaced.
    """
    conn.execute(
        "DELETE FROM revisions WHERE from_version_id IS ? AND to_version_id = ?",
        (from_version_id, to_version_id),
    )

    to_sections = db.list_sections(conn, to_version_id)
    from_sections = (
        db.list_sections(conn, from_version_id) if from_version_id is not None else []
    )
    changes, stats = compare_section_sets(from_sections, to_sections)

    cur = conn.execute(
        """
        INSERT INTO revisions(
            code_id, from_version_id, to_version_id,
            sections_added, sections_removed, sections_modified, sections_unchanged,
            summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code_id,
            from_version_id,
            to_version_id,
            stats.added,
            stats.removed,
            stats.modified,
            stats.unchanged,
            json.dumps(
                {
                    "added": stats.added,
                    "removed": stats.removed,
                    "modified": stats.modified,
                    "unchanged": stats.unchanged,
                }
            ),
            db.utcnow(),
        ),
    )
    revision_id = int(cur.lastrowid)

    for change in changes:
        conn.execute(
            """
            INSERT INTO section_changes(
                revision_id, change_type, identifier, level, num, heading,
                from_section_id, to_section_id, text_diff
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                change.change_type,
                change.identifier,
                change.level,
                change.num,
                change.heading,
                change.from_section_id,
                change.to_section_id,
                change.text_diff,
            ),
        )

    return RevisionStats(
        revision_id=revision_id,
        added=stats.added,
        removed=stats.removed,
        modified=stats.modified,
        unchanged=stats.unchanged,
    )
