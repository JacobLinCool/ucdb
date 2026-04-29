"""Structural diff computation for Akoma Ntoso expressions."""

from __future__ import annotations

import difflib
import json
import sqlite3
from dataclasses import dataclass

from . import db


@dataclass
class NodeChange:
    change_type: str  # added | removed | modified
    node_eid: str | None
    node_type: str
    num: str | None
    heading: str | None
    from_node_id: int | None
    to_node_id: int | None
    text_diff: str | None
    details: dict


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


def text_diff(before: str | None, after: str | None) -> str:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    if not a and not b:
        return ""
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n"
    return "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after", n=2))


def compare_node_sets(
    from_nodes: list[sqlite3.Row],
    to_nodes: list[sqlite3.Row],
) -> tuple[list[NodeChange], CompareStats]:
    from_by_eid = {row["node_eid"]: row for row in from_nodes if row["node_eid"]}
    to_by_eid = {row["node_eid"]: row for row in to_nodes if row["node_eid"]}
    changes: list[NodeChange] = []
    added = removed = modified = unchanged = 0

    for eid, to_row in to_by_eid.items():
        from_row = from_by_eid.get(eid)
        if from_row is None:
            added += 1
            changes.append(_change("added", to_row, None, to_row, None, {}))
            continue
        detail = _detail_changes(from_row, to_row)
        if not detail:
            unchanged += 1
            continue
        modified += 1
        changes.append(
            _change(
                "modified",
                to_row,
                from_row,
                to_row,
                text_diff(from_row["text"], to_row["text"])
                if "text_changed" in detail
                else "",
                {"events": detail},
            )
        )

    for eid, from_row in from_by_eid.items():
        if eid in to_by_eid:
            continue
        removed += 1
        changes.append(_change("removed", from_row, from_row, None, None, {}))

    return changes, CompareStats(added, removed, modified, unchanged)


def compute_revision(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    to_expression_id: int,
    from_expression_id: int | None,
) -> RevisionStats:
    conn.execute(
        "DELETE FROM revisions WHERE from_expression_id IS ? AND to_expression_id = ?",
        (from_expression_id, to_expression_id),
    )
    to_nodes = db.list_nodes(conn, to_expression_id)
    from_nodes = (
        db.list_nodes(conn, from_expression_id)
        if from_expression_id is not None
        else []
    )
    changes, stats = compare_node_sets(from_nodes, to_nodes)

    cur = conn.execute(
        """
        INSERT INTO revisions(
            work_id, from_expression_id, to_expression_id,
            nodes_added, nodes_removed, nodes_modified, nodes_unchanged,
            summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work_id,
            from_expression_id,
            to_expression_id,
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
            INSERT INTO node_changes(
                revision_id, change_type, node_eid, node_type, num, heading,
                from_node_id, to_node_id, text_diff, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                change.change_type,
                change.node_eid,
                change.node_type,
                change.num,
                change.heading,
                change.from_node_id,
                change.to_node_id,
                change.text_diff,
                json.dumps(change.details, ensure_ascii=False)
                if change.details
                else None,
            ),
        )
    return RevisionStats(
        revision_id, stats.added, stats.removed, stats.modified, stats.unchanged
    )


def _detail_changes(from_row: sqlite3.Row, to_row: sqlite3.Row) -> list[str]:
    events: list[str] = []
    if (from_row["parent_id"] is None) != (to_row["parent_id"] is None):
        events.append("node_moved")
    if (from_row["node_type"] or "") != (to_row["node_type"] or ""):
        events.append("type_changed")
    if (from_row["num"] or "") != (to_row["num"] or ""):
        events.append("num_changed")
    if (from_row["heading"] or "") != (to_row["heading"] or ""):
        events.append("heading_changed")
    if (from_row["normalized_text_hash"] or "") != (
        to_row["normalized_text_hash"] or ""
    ):
        events.append("text_changed")
    return events


def _change(
    change_type: str,
    snapshot: sqlite3.Row,
    from_row: sqlite3.Row | None,
    to_row: sqlite3.Row | None,
    diff: str | None,
    details: dict,
) -> NodeChange:
    return NodeChange(
        change_type=change_type,
        node_eid=snapshot["node_eid"],
        node_type=snapshot["node_type"],
        num=snapshot["num"],
        heading=snapshot["heading"],
        from_node_id=int(from_row["id"]) if from_row is not None else None,
        to_node_id=int(to_row["id"]) if to_row is not None else None,
        text_diff=diff,
        details=details,
    )
