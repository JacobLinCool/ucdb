"""Line-level provenance — the storage backing ``ucdb query blame``.

For each line of a section in version *V*, we record the version that *first
introduced that exact line* for the section's USLM identifier. Lines that
survive an edit unchanged inherit their origin from the predecessor; lines
that are added (or whose text is rewritten) are stamped with version *V*.

This mirrors how ``git blame`` works: each commit records, per line, the
commit that authored it; matched lines flow forward, and unmatched lines are
attributed to the new commit.

Sections without a USLM identifier cannot be aligned across versions, so all
their lines are stamped with the version they appear in.
"""

from __future__ import annotations

import difflib
import sqlite3

from . import db


def _split_lines(text: str | None) -> list[str]:
    """Split *text* into the non-blank content lines we attribute.

    Blank lines are skipped: they carry no legal content and would otherwise
    inflate line numbers without contributing any attributable text. Each
    surviving line is left-stripped of pure-whitespace prefixes but its inner
    structure (punctuation, casing, internal whitespace) is preserved so that
    SequenceMatcher only treats truly identical lines as equal.
    """
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()]


def compute_line_provenance(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    parent_version_id: int | None,
) -> int:
    """Populate ``section_lines`` for every section in *version_id*.

    Idempotent: any rows already attached to this version's sections are
    cleared first, so re-processing a version produces a clean rebuild.

    Returns the number of ``section_lines`` rows inserted.
    """
    conn.execute(
        """
        DELETE FROM section_lines
        WHERE section_id IN (SELECT id FROM sections WHERE version_id = ?)
        """,
        (version_id,),
    )

    sections = db.list_sections(conn, version_id)
    parent_lines_by_ident = _load_parent_lines(conn, parent_version_id)

    inserted = 0
    for section in sections:
        section_id = int(section["id"])
        new_lines = _split_lines(section["content"])
        ident = section["identifier"]
        parent_lines = parent_lines_by_ident.get(ident) if ident else None

        origins = _attribute_lines(
            new_lines,
            parent_lines,
            current_version_id=version_id,
            current_section_id=section_id,
        )

        for line_no, (text, (ov, os_)) in enumerate(zip(new_lines, origins), start=1):
            conn.execute(
                """
                INSERT INTO section_lines(
                    section_id, line_no, text, origin_version_id, origin_section_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (section_id, line_no, text, ov, os_),
            )
            inserted += 1

    return inserted


def _load_parent_lines(
    conn: sqlite3.Connection, parent_version_id: int | None
) -> dict[str, list[sqlite3.Row]]:
    """Return parent-version section_lines indexed by USLM identifier."""
    if parent_version_id is None:
        return {}
    parent_sections = db.list_sections(conn, parent_version_id)
    parent_by_ident = {r["identifier"]: r for r in parent_sections if r["identifier"]}
    if not parent_by_ident:
        return {}

    placeholders = ",".join("?" * len(parent_by_ident))
    cur = conn.execute(
        f"""
        SELECT section_id, line_no, text, origin_version_id, origin_section_id
        FROM section_lines
        WHERE section_id IN ({placeholders})
        ORDER BY section_id, line_no
        """,
        tuple(int(r["id"]) for r in parent_by_ident.values()),
    )
    by_section: dict[int, list[sqlite3.Row]] = {}
    for row in cur:
        by_section.setdefault(int(row["section_id"]), []).append(row)

    return {
        ident: by_section.get(int(parent_row["id"]), [])
        for ident, parent_row in parent_by_ident.items()
    }


def _attribute_lines(
    new_lines: list[str],
    parent_lines: list[sqlite3.Row] | None,
    *,
    current_version_id: int,
    current_section_id: int,
) -> list[tuple[int, int | None]]:
    """For each new line, return (origin_version_id, origin_section_id)."""
    fresh: tuple[int, int | None] = (current_version_id, current_section_id)
    if not parent_lines:
        return [fresh] * len(new_lines)

    parent_text = [r["text"] for r in parent_lines]
    sm = difflib.SequenceMatcher(a=parent_text, b=new_lines, autojunk=False)
    origins: list[tuple[int, int | None]] = [fresh] * len(new_lines)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            continue
        for offset in range(i2 - i1):
            prev = parent_lines[i1 + offset]
            origin_section = prev["origin_section_id"]
            origins[j1 + offset] = (
                int(prev["origin_version_id"]),
                int(origin_section) if origin_section is not None else None,
            )
    return origins
