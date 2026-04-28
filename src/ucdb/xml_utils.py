"""USLM XML parsing and validation helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from lxml import etree

USLM_NAMESPACE = "http://schemas.gpo.gov/xml/uslm"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"

# USLM hierarchical container elements that we want to surface as rows in the
# `sections` table. Sourced from the USLM 2.x schema; we use local-name matching
# so documents authored without namespace prefixes still work.
HIERARCHICAL_LEVELS: frozenset[str] = frozenset(
    {
        "title",
        "subtitle",
        "division",
        "subdivision",
        "chapter",
        "subchapter",
        "part",
        "subpart",
        "article",
        "subarticle",
        "section",
        "subsection",
        "paragraph",
        "subparagraph",
        "clause",
        "subclause",
        "item",
        "subitem",
        "preamble",
        "recitals",
        "provisions",
        "appendix",
        "schedule",
    }
)


class XMLValidationError(ValueError):
    """Raised when XML fails well-formedness or USLM structural checks."""


def parse_xml(text: str | bytes) -> etree._Element:
    """Parse *text* into an lxml element, raising :class:`XMLValidationError`."""
    if isinstance(text, str):
        data = text.encode("utf-8")
    else:
        data = text
    parser = etree.XMLParser(remove_blank_text=False, recover=False)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise XMLValidationError(f"XML is not well-formed: {exc}") from exc


def local_name(elem: etree._Element) -> str:
    tag = elem.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def namespace(elem: etree._Element) -> str:
    tag = elem.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def is_uslm_element(elem: etree._Element) -> bool:
    """True if *elem* is in the USLM namespace or has no namespace at all.

    USLM documents are typically authored either in the canonical namespace or
    namespace-free; either should be accepted. Elements in foreign namespaces
    (e.g. ``dc:``) must NOT be treated as USLM containers even if their local
    name happens to collide with a USLM level (``dc:title``).
    """
    ns = namespace(elem)
    return ns == "" or ns == USLM_NAMESPACE


def validate_uslm_structure(root: etree._Element) -> None:
    """Run lightweight structural checks against a parsed USLM document."""
    if local_name(root) != "uslm":
        raise XMLValidationError(
            f"Root element must be <uslm>, found <{local_name(root)}>"
        )
    has_main = any(local_name(child) == "main" for child in root.iter())
    if not has_main:
        raise XMLValidationError("USLM document is missing a <main> element")


def validate_against_xsd(
    root: etree._Element, xsd_path: Path | str | None = None
) -> None:
    """Validate *root* against an XSD if one is configured.

    The schema location can be passed explicitly or via the ``UCDB_USLM_XSD``
    environment variable. When neither is set, this is a no-op.
    """
    target = xsd_path or os.environ.get("UCDB_USLM_XSD")
    if not target:
        return
    schema_doc = etree.parse(str(target))
    schema = etree.XMLSchema(schema_doc)
    if not schema.validate(root):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise XMLValidationError(f"USLM schema validation failed: {errors}")


def is_hierarchical_level(elem: etree._Element) -> bool:
    return is_uslm_element(elem) and local_name(elem) in HIERARCHICAL_LEVELS


def iter_levels(root: etree._Element) -> Iterable[etree._Element]:
    """Yield each hierarchical USLM element in document order."""
    for elem in root.iter():
        if is_hierarchical_level(elem):
            yield elem


def text_of_child(elem: etree._Element, name: str) -> str | None:
    for child in elem:
        if is_uslm_element(child) and local_name(child) == name:
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def collect_content_text(elem: etree._Element) -> str | None:
    """Collect immediate <content>/<p> text for a hierarchical element.

    Each ``<p>`` is extracted individually and stripped, then joined with a
    single newline. This keeps per-line text clean — the indentation inserted
    between sibling ``<p>`` tags by pretty-printers does not leak into stored
    content (and therefore does not leak into line-level blame either).
    Multiple ``<content>`` blocks are separated by a blank line.
    """
    parts: list[str] = []
    for child in elem:
        if is_uslm_element(child) and local_name(child) == "content":
            paragraphs = [
                "".join(p.itertext()).strip()
                for p in child
                if is_uslm_element(p) and local_name(p) == "p"
            ]
            paragraphs = [p for p in paragraphs if p]
            if paragraphs:
                parts.append("\n".join(paragraphs))
            else:
                text = "".join(child.itertext()).strip()
                if text:
                    parts.append(text)
    if not parts:
        for child in elem:
            if is_uslm_element(child) and local_name(child) == "p":
                text = "".join(child.itertext()).strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts) if parts else None


def serialize(elem: etree._Element) -> str:
    return etree.tostring(elem, pretty_print=True, encoding="unicode")
