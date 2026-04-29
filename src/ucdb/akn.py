"""Akoma Ntoso / LegalDocML parsing and serialization."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from lxml import etree

from .model import LegalDocument, LegalNode
from .tw_profile import AKN_PROFILE

AKN_NAMESPACE = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"

DOCUMENT_ELEMENTS = {"act", "doc", "bill", "judgment", "portion"}
BODY_ELEMENTS = {"body", "mainBody", "portionBody", "judgmentBody"}
HIERARCHICAL_ELEMENTS = {
    "part",
    "chapter",
    "section",
    "article",
    "paragraph",
    "subparagraph",
    "point",
    "indent",
    "hcontainer",
    "attachment",
    "appendix",
    "schedule",
}


class AKNValidationError(ValueError):
    """Raised when Akoma Ntoso XML fails well-formedness or profile checks."""


def parse_xml(text: str | bytes) -> etree._Element:
    data = text.encode("utf-8") if isinstance(text, str) else text
    parser = etree.XMLParser(remove_blank_text=False, recover=False)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise AKNValidationError(f"XML is not well-formed: {exc}") from exc


def local_name(elem: etree._Element) -> str:
    tag = elem.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def namespace(elem: etree._Element) -> str:
    tag = elem.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def is_akn_element(elem: etree._Element) -> bool:
    ns = namespace(elem)
    return ns == "" or ns == AKN_NAMESPACE


def qn(name: str) -> str:
    return f"{{{AKN_NAMESPACE}}}{name}"


def validate_akn_structure(root: etree._Element) -> None:
    if local_name(root) != "akomaNtoso":
        raise AKNValidationError(
            f"Root element must be <akomaNtoso>, found <{local_name(root)}>"
        )
    document = document_element(root)
    if document is None:
        raise AKNValidationError("Akoma Ntoso document is missing an act/doc element")
    if body_element(document) is None:
        raise AKNValidationError("Akoma Ntoso document is missing a body element")


def validate_against_xsd(
    root: etree._Element, xsd_path: Path | str | None = None
) -> None:
    target = xsd_path or os.environ.get("UCDB_AKN_XSD")
    if not target:
        return
    schema_doc = etree.parse(str(target))
    schema = etree.XMLSchema(schema_doc)
    if not schema.validate(root):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise AKNValidationError(f"Akoma Ntoso schema validation failed: {errors}")


def document_element(root: etree._Element) -> etree._Element | None:
    for child in root:
        if is_akn_element(child) and local_name(child) in DOCUMENT_ELEMENTS:
            return child
    return None


def body_element(document: etree._Element) -> etree._Element | None:
    for child in document:
        if is_akn_element(child) and local_name(child) in BODY_ELEMENTS:
            return child
    return None


def is_hierarchical_node(elem: etree._Element) -> bool:
    return is_akn_element(elem) and local_name(elem) in HIERARCHICAL_ELEMENTS


def iter_nodes(root: etree._Element) -> Iterable[etree._Element]:
    doc = document_element(root)
    body = body_element(doc) if doc is not None else None
    if body is None:
        return
    for elem in body.iter():
        if is_hierarchical_node(elem):
            yield elem


def text_of_child(elem: etree._Element, name: str) -> str | None:
    for child in elem:
        if is_akn_element(child) and local_name(child) == name:
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def collect_content_text(elem: etree._Element) -> str | None:
    parts: list[str] = []
    for child in elem:
        if is_akn_element(child) and local_name(child) == "content":
            paragraphs = [
                "".join(p.itertext()).strip()
                for p in child
                if is_akn_element(p) and local_name(p) in {"p", "block"}
            ]
            paragraphs = [p for p in paragraphs if p]
            if paragraphs:
                parts.append("\n".join(paragraphs))
            else:
                text = "".join(child.itertext()).strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts) if parts else None


def serialize_element(elem: etree._Element) -> str:
    return etree.tostring(elem, pretty_print=True, encoding="unicode")


def serialize_document(document: LegalDocument) -> str:
    nsmap = {None: AKN_NAMESPACE}
    root = etree.Element(qn("akomaNtoso"), nsmap=nsmap)
    act = etree.SubElement(root, qn("act"), name=document.document_class)
    act.set("contains", "originalVersion")
    act.set("ucdbProfile", AKN_PROFILE)
    _emit_meta(act, document)
    body = etree.SubElement(act, qn("body"))
    for node in document.nodes:
        _emit_node(body, node)
    return etree.tostring(root, pretty_print=True, encoding="unicode")


def _emit_meta(parent: etree._Element, document: LegalDocument) -> None:
    meta = etree.SubElement(parent, qn("meta"))
    identification = etree.SubElement(meta, qn("identification"), source="#ucdb")

    work = etree.SubElement(identification, qn("FRBRWork"))
    etree.SubElement(work, qn("FRBRthis"), value=document.work_uri + "/!main")
    etree.SubElement(work, qn("FRBRuri"), value=document.work_uri)
    etree.SubElement(
        work,
        qn("FRBRdate"),
        date=document.expression_date or document.version_label,
        name=document.title or document.work_id,
    )
    etree.SubElement(work, qn("FRBRauthor"), href="#source", **{"as": "#author"})
    etree.SubElement(work, qn("FRBRcountry"), value=document.jurisdiction)
    etree.SubElement(work, qn("FRBRsubtype"), value=document.document_class)
    etree.SubElement(work, qn("FRBRname"), value=document.title or document.work_id)

    expression = etree.SubElement(identification, qn("FRBRExpression"))
    etree.SubElement(
        expression, qn("FRBRthis"), value=document.expression_uri + "/!main"
    )
    etree.SubElement(expression, qn("FRBRuri"), value=document.expression_uri)
    etree.SubElement(
        expression,
        qn("FRBRdate"),
        date=document.expression_date or document.version_label,
        name=document.version_label,
    )
    etree.SubElement(expression, qn("FRBRauthor"), href="#ucdb", **{"as": "#editor"})
    etree.SubElement(expression, qn("FRBRlanguage"), language=document.language)

    manifestation = etree.SubElement(identification, qn("FRBRManifestation"))
    etree.SubElement(
        manifestation, qn("FRBRthis"), value=document.expression_uri + "/!main.xml"
    )
    etree.SubElement(
        manifestation, qn("FRBRuri"), value=document.expression_uri + ".akn"
    )
    etree.SubElement(
        manifestation,
        qn("FRBRdate"),
        date=document.expression_date or document.version_label,
        name="UCDB Akoma Ntoso XML",
    )
    etree.SubElement(
        manifestation, qn("FRBRauthor"), href="#ucdb", **{"as": "#generator"}
    )

    references = etree.SubElement(meta, qn("references"), source="#ucdb")
    etree.SubElement(
        references,
        qn("TLCOrganization"),
        eId="ucdb",
        href="/akn/tw/ontology/organization/ucdb",
        showAs="Universal Code Database",
    )
    etree.SubElement(
        references,
        qn("TLCOrganization"),
        eId="source",
        href=document.source_url or f"/akn/{document.jurisdiction}/source",
        showAs=document.source_authority or "Source authority",
    )


def _emit_node(parent: etree._Element, node: LegalNode) -> None:
    element_name = (
        node.node_type if node.node_type in HIERARCHICAL_ELEMENTS else "hcontainer"
    )
    attrib = {"eId": node.eId}
    if element_name == "hcontainer":
        attrib["name"] = node.profile_type or node.node_type
    elem = etree.SubElement(parent, qn(element_name), attrib=attrib)
    if node.profile_type and element_name != "hcontainer":
        elem.set("class", node.profile_type)
    if node.source_locator:
        elem.set("ucdbSource", node.source_locator)
    if node.num:
        etree.SubElement(elem, qn("num")).text = node.num
    if node.heading:
        etree.SubElement(elem, qn("heading")).text = node.heading
    body = [p for p in node.paragraphs if p and p.strip()]
    if body:
        content = etree.SubElement(elem, qn("content"))
        for paragraph in body:
            etree.SubElement(content, qn("p")).text = paragraph
    for child in node.children:
        _emit_node(elem, child)
