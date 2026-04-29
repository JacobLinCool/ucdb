"""Line-level provenance for UCDB nodes."""

from __future__ import annotations

import difflib
import sqlite3

from . import db


def compute_line_provenance(
    conn: sqlite3.Connection,
    *,
    expression_id: int,
    parent_expression_id: int | None,
) -> int:
    conn.execute(
        """
        DELETE FROM node_lines
        WHERE node_id IN (SELECT id FROM nodes WHERE expression_id = ?)
        """,
        (expression_id,),
    )
    nodes = db.list_nodes(conn, expression_id)
    parent_lines_by_eid = _load_parent_lines(conn, parent_expression_id)

    inserted = 0
    for node in nodes:
        node_id = int(node["id"])
        new_lines = _split_lines(node["text"])
        parent_lines = parent_lines_by_eid.get(node["node_eid"])
        origins = _attribute_lines(
            new_lines,
            parent_lines,
            current_expression_id=expression_id,
            current_node_id=node_id,
        )
        for line_no, (text, (oe, on)) in enumerate(zip(new_lines, origins), start=1):
            conn.execute(
                """
                INSERT INTO node_lines(
                    node_id, line_no, text, origin_expression_id, origin_node_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (node_id, line_no, text, oe, on),
            )
            inserted += 1
    return inserted


def _split_lines(text: str | None) -> list[str]:
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _load_parent_lines(
    conn: sqlite3.Connection, parent_expression_id: int | None
) -> dict[str, list[sqlite3.Row]]:
    if parent_expression_id is None:
        return {}
    parent_nodes = db.list_nodes(conn, parent_expression_id)
    parent_by_eid = {row["node_eid"]: row for row in parent_nodes if row["node_eid"]}
    if not parent_by_eid:
        return {}
    placeholders = ",".join("?" * len(parent_by_eid))
    rows = conn.execute(
        f"""
        SELECT node_id, line_no, text, origin_expression_id, origin_node_id
        FROM node_lines
        WHERE node_id IN ({placeholders})
        ORDER BY node_id, line_no
        """,
        tuple(int(row["id"]) for row in parent_by_eid.values()),
    )
    by_node: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_node.setdefault(int(row["node_id"]), []).append(row)
    return {
        eid: by_node.get(int(parent["id"]), []) for eid, parent in parent_by_eid.items()
    }


def _attribute_lines(
    new_lines: list[str],
    parent_lines: list[sqlite3.Row] | None,
    *,
    current_expression_id: int,
    current_node_id: int,
) -> list[tuple[int, int | None]]:
    fresh: tuple[int, int | None] = (current_expression_id, current_node_id)
    if not parent_lines:
        return [fresh] * len(new_lines)
    parent_text = [row["text"] for row in parent_lines]
    matcher = difflib.SequenceMatcher(a=parent_text, b=new_lines, autojunk=False)
    origins: list[tuple[int, int | None]] = [fresh] * len(new_lines)
    for op, i1, i2, j1, _j2 in matcher.get_opcodes():
        if op != "equal":
            continue
        for offset in range(i2 - i1):
            prev = parent_lines[i1 + offset]
            origin_node = prev["origin_node_id"]
            origins[j1 + offset] = (
                int(prev["origin_expression_id"]),
                int(origin_node) if origin_node is not None else None,
            )
    return origins
