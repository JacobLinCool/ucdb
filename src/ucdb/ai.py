"""AI-assisted conversion of legal text into Akoma Ntoso XML."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from .akn import serialize_document
from .model import LegalDocument, LegalNode

NodeKind = Literal[
    "part",
    "chapter",
    "section",
    "article",
    "paragraph",
    "point",
    "indent",
    "attachment",
    "schedule",
    "appendix",
]

_EID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z][A-Za-z0-9_]*)*$")

SYSTEM_PROMPT = """You convert legal documents into a Taiwan profile of Akoma \
Ntoso / LegalDocML. Return a structured tree only; Python will serialize XML.

Rules:
- Preserve text verbatim. Never summarize, translate, or paraphrase.
- Use node kinds from: part, chapter, section, article, paragraph, point, \
indent, attachment, schedule, appendix.
- Taiwan mapping: 編=part, 章=chapter, 節=section, 條=article, 項=paragraph, \
款=point, 目=indent.
- num keeps the visible legal number exactly as written, including values such \
as 第十二條之一.
- eId is stable, ASCII, expression-local, and unique. Examples: art_12, \
art_12_1, art_12__para_2, chp_1__sec_2.
- Child eIds should start with parent eId + "__" when practical.
- paragraphs contains only body text directly belonging to that node.
- If a prior Akoma Ntoso expression is provided, reuse eIds for surviving \
provisions even if display numbers or headings changed.
"""

USER_TEMPLATE = """Convert the document into the UCDB Taiwan LegalDocML tree.

work-id: {work_id}
version: {version_label}
language: {language}
{parent_block}
--- BEGIN DOCUMENT ---
{document}
--- END DOCUMENT ---
"""

PARENT_TEMPLATE = """
--- BEGIN PRIOR AKOMA NTOSO REFERENCE ({parent_label}) ---
{parent_xml}
--- END PRIOR AKOMA NTOSO REFERENCE ---
"""


class NodeOut(BaseModel):
    kind: NodeKind
    eId: str
    num: str | None = None
    heading: str | None = None
    paragraphs: list[str] = Field(default_factory=list)
    children: list["NodeOut"] = Field(default_factory=list)


NodeOut.model_rebuild()


class DocumentOut(BaseModel):
    title: str | None = None
    document_class: str = "law"
    jurisdiction: str = "tw"
    language: str = "zho"
    expression_date: str | None = None
    source_authority: str | None = None
    nodes: list[NodeOut]


@dataclass
class AIConfig:
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-5.4-mini"
    reasoning_effort: str = "medium"
    max_input_chars: int = 200_000
    max_parent_xml_chars: int = 60_000
    validation_retries: int = 3

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("UCDB_MODEL", "gpt-5.4-mini"),
        )

    def provider_name(self) -> str:
        if not self.base_url:
            return "openai"
        url = self.base_url.lower()
        if "googleapis" in url or "generativelanguage" in url:
            return "google"
        if "ollama" in url or ":11434" in url:
            return "ollama"
        if "azure" in url:
            return "azure-openai"
        try:
            from urllib.parse import urlparse

            return urlparse(self.base_url).hostname or "custom"
        except Exception:
            return "custom"


class AIError(RuntimeError):
    pass


@dataclass
class AIUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class AIResult:
    xml: str
    usage: AIUsage


def generate_akn_xml(
    text: str,
    *,
    work_id: str,
    version_label: str,
    language: str = "zho",
    config: AIConfig | None = None,
    parent_xml: str | None = None,
    parent_label: str | None = None,
) -> AIResult:
    cfg = config or AIConfig.from_env()
    if not cfg.api_key:
        raise AIError("OPENAI_API_KEY is not set.")
    if not text.strip():
        raise AIError("Document text is empty; nothing to convert.")

    document = text[: cfg.max_input_chars]
    parent_block = ""
    if parent_xml:
        parent_block = PARENT_TEMPLATE.format(
            parent_label=parent_label or "previous",
            parent_xml=parent_xml[: cfg.max_parent_xml_chars],
        )

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AIError("openai package is not installed") from exc

    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.format(
                work_id=work_id,
                version_label=version_label,
                language=language,
                parent_block=parent_block,
                document=document,
            ),
        },
    ]

    usage_total = AIUsage()
    parsed: DocumentOut | None = None
    last_violations: list[str] = []
    for attempt in range(max(1, cfg.validation_retries + 1)):
        response = client.chat.completions.parse(
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            response_format=DocumentOut,
            messages=messages,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            usage_total.prompt_tokens = (usage_total.prompt_tokens or 0) + (
                getattr(usage, "prompt_tokens", 0) or 0
            )
            usage_total.completion_tokens = (usage_total.completion_tokens or 0) + (
                getattr(usage, "completion_tokens", 0) or 0
            )
            usage_total.total_tokens = (usage_total.total_tokens or 0) + (
                getattr(usage, "total_tokens", 0) or 0
            )
        candidate = response.choices[0].message.parsed
        if candidate is None:
            raise AIError("AI did not return structured Akoma Ntoso output.")
        last_violations = _validate_document(candidate)
        if not last_violations:
            parsed = candidate
            break
        if attempt + 1 < max(1, cfg.validation_retries + 1):
            messages.extend(
                [
                    {"role": "assistant", "content": candidate.model_dump_json()},
                    {
                        "role": "user",
                        "content": "Fix these eId issues:\n- "
                        + "\n- ".join(last_violations),
                    },
                ]
            )
    if parsed is None:
        raise AIError("AI output failed validation: " + "; ".join(last_violations))

    legal_doc = LegalDocument(
        work_id=work_id,
        version_label=version_label,
        title=parsed.title,
        jurisdiction=parsed.jurisdiction,
        document_class=parsed.document_class,
        language=parsed.language or language,
        expression_date=parsed.expression_date or version_label,
        source_authority=parsed.source_authority,
        nodes=[_to_node(node) for node in parsed.nodes],
    )
    return AIResult(xml=serialize_document(legal_doc), usage=usage_total)


def _validate_document(document: DocumentOut) -> list[str]:
    seen: set[str] = set()
    violations: list[str] = []

    def visit(node: NodeOut, parent: str | None) -> None:
        if not _EID_RE.match(node.eId):
            violations.append(f"invalid eId {node.eId!r}")
        if node.eId in seen:
            violations.append(f"duplicate eId {node.eId!r}")
        seen.add(node.eId)
        if parent and not node.eId.startswith(parent + "__"):
            violations.append(f"child eId {node.eId!r} is not under parent {parent!r}")
        for child in node.children:
            visit(child, node.eId)

    for top in document.nodes:
        visit(top, None)
    return violations


def _to_node(node: NodeOut) -> LegalNode:
    return LegalNode(
        node_type=node.kind,
        eId=node.eId,
        num=node.num,
        heading=node.heading,
        paragraphs=node.paragraphs,
        children=[_to_node(child) for child in node.children],
    )
