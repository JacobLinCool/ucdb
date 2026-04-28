"""Import a parsed USLM document into the database."""

from __future__ import annotations

import sqlite3

from lxml import etree

from . import db
from .xml_utils import (
    collect_content_text,
    is_hierarchical_level,
    local_name,
    parse_xml,
    serialize,
    text_of_child,
    validate_against_xsd,
    validate_uslm_structure,
)


def ingest_xml(
    conn: sqlite3.Connection,
    version_id: int,
    xml_text: str,
    *,
    validate_schema: bool = True,
) -> int:
    """Validate *xml_text*, replace any prior sections for *version_id*, and insert.

    Returns the number of section rows inserted.
    """
    root = parse_xml(xml_text)
    validate_uslm_structure(root)
    if validate_schema:
        validate_against_xsd(root)

    db.clear_version_sections(conn, version_id)
    db.set_version_status(conn, version_id, "imported", xml_content=xml_text)

    counter = {"ord": 0, "count": 0}
    parent_stack: list[tuple[etree._Element, int]] = []

    def is_descendant_of(target: etree._Element, ancestor: etree._Element) -> bool:
        node = target.getparent()
        while node is not None:
            if node is ancestor:
                return True
            node = node.getparent()
        return False

    for elem in root.iter():
        if not is_hierarchical_level(elem):
            continue

        while parent_stack and not is_descendant_of(elem, parent_stack[-1][0]):
            parent_stack.pop()

        parent_id = parent_stack[-1][1] if parent_stack else None
        counter["ord"] += 1
        section_id = db.insert_section(
            conn,
            version_id=version_id,
            parent_id=parent_id,
            level=local_name(elem),
            identifier=elem.get("identifier") or elem.get("id"),
            num=text_of_child(elem, "num"),
            heading=text_of_child(elem, "heading"),
            content=collect_content_text(elem),
            xml_fragment=serialize(elem),
            ordering=counter["ord"],
        )
        counter["count"] += 1
        parent_stack.append((elem, section_id))

    return counter["count"]
