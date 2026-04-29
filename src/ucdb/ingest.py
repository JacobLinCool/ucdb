"""Import Akoma Ntoso XML into normalized UCDB tables."""

from __future__ import annotations

import sqlite3

from lxml import etree

from . import db, hashing
from .akn import (
    AKNValidationError,
    collect_content_text,
    is_hierarchical_node,
    local_name,
    parse_xml,
    serialize_element,
    text_of_child,
    validate_against_xsd,
    validate_akn_structure,
)
from .tw_profile import normalize_text


def ingest_akn_xml(
    conn: sqlite3.Connection,
    expression_id: int,
    xml_text: str,
    *,
    validate_schema: bool = True,
) -> int:
    """Validate Akoma Ntoso XML and replace nodes for *expression_id*."""
    root = parse_xml(xml_text)
    validate_akn_structure(root)
    if validate_schema:
        validate_against_xsd(root)

    db.clear_expression_nodes(conn, expression_id)
    db.set_expression_status(conn, expression_id, "imported", canonical_xml=xml_text)

    counter = {"ord": 0, "count": 0}
    parent_stack: list[tuple[etree._Element, int, int]] = []

    def is_descendant_of(target: etree._Element, ancestor: etree._Element) -> bool:
        node = target.getparent()
        while node is not None:
            if node is ancestor:
                return True
            node = node.getparent()
        return False

    for elem in root.iter():
        if not is_hierarchical_node(elem):
            continue
        while parent_stack and not is_descendant_of(elem, parent_stack[-1][0]):
            parent_stack.pop()

        parent_id = parent_stack[-1][1] if parent_stack else None
        depth = parent_stack[-1][2] + 1 if parent_stack else 0
        node_type = (
            elem.get("name") if local_name(elem) == "hcontainer" else local_name(elem)
        )
        node_eid = elem.get("eId")
        if not node_eid:
            raise AKNValidationError(f"<{local_name(elem)}> is missing required eId")
        text = collect_content_text(elem)
        normalized = normalize_text(text)
        counter["ord"] += 1
        node_id = db.insert_node(
            conn,
            expression_id=expression_id,
            parent_id=parent_id,
            node_eid=node_eid,
            node_type=node_type or local_name(elem),
            profile_type=elem.get("class") or elem.get("name"),
            num=text_of_child(elem, "num"),
            heading=text_of_child(elem, "heading"),
            text=text,
            xml_fragment=serialize_element(elem),
            text_hash=hashing.hash_text(text or "") if text else None,
            normalized_text_hash=hashing.hash_text(normalized) if normalized else None,
            ordering=counter["ord"],
            depth=depth,
            source_locator=elem.get("ucdbSource"),
        )
        _insert_blocks(conn, node_id, elem)
        counter["count"] += 1
        parent_stack.append((elem, node_id, depth))

    return counter["count"]


def _insert_blocks(
    conn: sqlite3.Connection, node_id: int, elem: etree._Element
) -> None:
    ordering = 0
    for child in elem:
        if local_name(child) != "content":
            continue
        for block in child:
            if local_name(block) not in {"p", "block"}:
                continue
            text = "".join(block.itertext()).strip()
            normalized = normalize_text(text)
            ordering += 1
            db.insert_node_block(
                conn,
                node_id=node_id,
                block_eid=block.get("eId"),
                block_type=block.get("name") or local_name(block),
                text=text,
                xml_fragment=serialize_element(block),
                ordering=ordering,
                text_hash=hashing.hash_text(text) if text else None,
                normalized_text_hash=hashing.hash_text(normalized)
                if normalized
                else None,
            )
