"""AI-assisted conversion of plain text into United States Legislative Markup XML.

The model returns a structured tree (Pydantic / OpenAI Structured Outputs) and
this module serialises that tree into USLM XML. Asking for a tree instead of
raw XML keeps the model from drifting on element names and namespaces, and
lets the Python side enforce identifier shape, parent/child nesting, and
uniqueness with a one-shot retry.

Uses an OpenAI-compatible API. Set:

* ``OPENAI_API_KEY``  — API key (required).
* ``OPENAI_BASE_URL`` — endpoint, defaults to OpenAI. Use this to point at
  Gemini, Ollama, or any other OpenAI-compatible service.
* ``UCDB_MODEL``      — model id (default: ``gpt-5.4-mini``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from lxml import etree
from pydantic import BaseModel, Field

USLM_NAMESPACE = "http://schemas.gpo.gov/xml/uslm"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"

ContainerKind = Literal[
    "title",
    "subtitle",
    "chapter",
    "subchapter",
    "part",
    "subpart",
    "section",
    "subsection",
    "paragraph",
    "subparagraph",
    "clause",
    "subclause",
]

# Container identifiers must be ASCII path-safe so they can serve as stable
# cross-version join keys without normalisation surprises.
_IDENTIFIER_RE = re.compile(r"^/[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")


SYSTEM_PROMPT = """You convert raw legal/legislative documents into a structured \
representation of United States Legislative Markup (USLM). The Python caller \
will serialize your tree into XML — focus on the structure, do not write XML.

Output rules:

- Top-level `children` lists the highest-level containers in the source. Do \
NOT add a wrapper title/chapter unless the source actually has one.

- `kind` MUST be one of: title, subtitle, chapter, subchapter, part, subpart, \
section, subsection, paragraph, subparagraph, clause, subclause. Pick by \
source structure (typical ROC convention: 章 → chapter, 節 → subchapter, \
條 → section, 項 → subsection, 款 → paragraph, 目 → subparagraph).

- Every container needs a stable, deterministic `identifier`:
  * Form: `/<code-id>/<token>[/<token>]...`. ASCII letters, digits, `-`, `_` \
only — never raw Chinese characters.
  * Level letters at and above section: t (title), st (subtitle), c (chapter), \
sc (subchapter), p (part), sp (subpart), s (section). Examples `/code/t1`, \
`/code/c2`, `/code/s5`.
  * BELOW section, use the original numbering token directly without a level \
letter. Examples `/code/s5/a`, `/code/s5/a/1`, `/code/s5/a/1/A`.
  * Each child's identifier MUST start with the parent's identifier + `/`. \
Identifiers must be unique within the document.

- Map Chinese / fullwidth numbering to ASCII tokens for identifiers. The \
original glyph stays in `num` verbatim — only the identifier is normalised:

    第一條 第二條 ...      →  s1, s2, ...
    第一章 第二章 ...      →  c1, c2, ...
    一、 二、 三、 ...     →  1, 2, 3, ...
    （一）（二）（三）...  →  A, B, C, ...
    （甲）（乙）（丙）...  →  A, B, C, ...
    壹、 貳、 參、 ...     →  1, 2, 3, ...

  Apply this deterministically: the same source numbering at the same depth \
ALWAYS produces the same token across runs and across versions.

- `num` and `heading` keep the original glyphs verbatim (whitespace and \
punctuation faithful). Set to null if absent — never invent.

- `paragraphs` is the body text of THIS container only, one entry per source \
paragraph, plain text. Do not include nested containers' text. Do not split \
a paragraph into bullet items.

- Never summarise, paraphrase, or translate the source.

- If a PRIOR-VERSION REFERENCE is provided, REUSE its identifiers for any \
provision that survives, even when num/heading changed. Stability across \
versions matters more than literal correspondence to the new numbering.
"""


USER_TEMPLATE = """Convert the following document into the structured USLM tree.

code-id: {code_id}
version: {version_label}
{parent_block}
--- BEGIN DOCUMENT ---
{document}
--- END DOCUMENT ---
"""

PARENT_TEMPLATE = """
--- BEGIN PRIOR-VERSION REFERENCE ({parent_label}) ---
{parent_xml}
--- END PRIOR-VERSION REFERENCE ---

Reuse identifiers from the prior version for provisions that survive in the \
new document, so cross-version diffs and blame line up.
"""


class Container(BaseModel):
    """One USLM container node, recursively nestable."""

    kind: ContainerKind
    identifier: str = Field(
        description=(
            "Stable hierarchical id starting with /<code-id>/. ASCII only. "
            "Examples: /code/s1, /code/s1/a, /code/c1/s2."
        )
    )
    num: str | None = Field(
        description="Original numbering glyph verbatim. null if absent."
    )
    heading: str | None = Field(description="Heading text verbatim. null if absent.")
    paragraphs: list[str] = Field(
        description="Plain-text paragraphs of THIS container's body, in order."
    )
    children: list["Container"] = Field(
        description="Nested child containers, in document order."
    )


Container.model_rebuild()


class USLMTree(BaseModel):
    """Top-level structured USLM output."""

    document_title: str = Field(description="Document title verbatim.")
    document_type: str = Field(
        description="Short type label (e.g. Regulation, Statute, Rule, Bylaw)."
    )
    document_identifier: str = Field(
        description="Stable id for the whole document (usually /<code-id>)."
    )
    children: list[Container] = Field(
        description="Top-level containers in document order."
    )


@dataclass
class AIConfig:
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-5.4-mini"
    reasoning_effort: str = "medium"
    max_input_chars: int = 200_000
    # Maximum number of characters of the prior-version XML to include as a
    # reference when generating a new version. Capped to prevent prompt blowup
    # on long codes; the model will get the head of the parent XML, which is
    # where `<meta>` and the bulk of stable identifiers live.
    max_parent_xml_chars: int = 60_000
    # Number of times to re-prompt with structural feedback if the model
    # returns a tree that violates identifier rules. 0 disables retries.
    validation_retries: int = 3

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("UCDB_MODEL", "gpt-5.4-mini"),
        )

    def provider_name(self) -> str:
        """Best-effort identification of the AI provider for provenance."""
        if not self.base_url:
            return "openai"
        url = self.base_url.lower()
        if "googleapis" in url or "generativelanguage" in url:
            return "google"
        if "ollama" in url or ":11434" in url:
            return "ollama"
        if "anthropic" in url:
            return "anthropic"
        if "azure" in url:
            return "azure-openai"
        try:
            from urllib.parse import urlparse

            host = urlparse(self.base_url).hostname or ""
            return host or "custom"
        except Exception:
            return "custom"


class AIError(RuntimeError):
    pass


@dataclass
class AIUsage:
    """Token-accounting numbers reported by the AI backend, when available."""

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


# ---------- structural validation ----------


def _walk_with_parent(tree: USLMTree):
    """Yield (node, parent_identifier_or_None) in document order."""
    stack: list[tuple[Container, str | None]] = [
        (c, None) for c in reversed(tree.children)
    ]
    while stack:
        node, parent_id = stack.pop()
        yield node, parent_id
        for child in reversed(node.children):
            stack.append((child, node.identifier))


def _validate_tree(tree: USLMTree) -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    for node, parent_id in _walk_with_parent(tree):
        ident = node.identifier
        if not _IDENTIFIER_RE.match(ident):
            violations.append(
                f"identifier {ident!r} ({node.kind}) is not ASCII path-safe; "
                "it must match /<token>(/<token>)*."
            )
        if ident in seen:
            violations.append(f"duplicate identifier {ident!r}.")
        seen.add(ident)
        if parent_id is not None and not ident.startswith(parent_id + "/"):
            violations.append(
                f"child {ident!r} ({node.kind}) is not nested under parent "
                f"{parent_id!r}; child identifier must start with parent + '/'."
            )
    return violations


# ---------- USLM XML serialisation ----------


def _serialize_tree(tree: USLMTree) -> str:
    nsmap = {None: USLM_NAMESPACE, "dc": DC_NAMESPACE}
    root = etree.Element(f"{{{USLM_NAMESPACE}}}uslm", nsmap=nsmap)

    meta = etree.SubElement(root, f"{{{USLM_NAMESPACE}}}meta")
    if tree.document_title:
        etree.SubElement(meta, f"{{{DC_NAMESPACE}}}title").text = tree.document_title
    if tree.document_type:
        etree.SubElement(meta, f"{{{DC_NAMESPACE}}}type").text = tree.document_type
    if tree.document_identifier:
        etree.SubElement(
            meta, f"{{{DC_NAMESPACE}}}identifier"
        ).text = tree.document_identifier

    main = etree.SubElement(root, f"{{{USLM_NAMESPACE}}}main")
    for top in tree.children:
        _emit_container(main, top)

    return etree.tostring(root, pretty_print=True, encoding="unicode")


def _emit_container(parent: etree._Element, node: Container) -> None:
    elem = etree.SubElement(
        parent,
        f"{{{USLM_NAMESPACE}}}{node.kind}",
        attrib={"identifier": node.identifier},
    )
    if node.num:
        etree.SubElement(elem, f"{{{USLM_NAMESPACE}}}num").text = node.num
    if node.heading:
        etree.SubElement(elem, f"{{{USLM_NAMESPACE}}}heading").text = node.heading
    body = [p for p in node.paragraphs if p and p.strip()]
    if body:
        content = etree.SubElement(elem, f"{{{USLM_NAMESPACE}}}content")
        for p in body:
            etree.SubElement(content, f"{{{USLM_NAMESPACE}}}p").text = p
    for child in node.children:
        _emit_container(elem, child)


# ---------- entrypoint ----------


def generate_uslm_xml(
    text: str,
    *,
    code_id: str,
    version_label: str,
    config: AIConfig | None = None,
    parent_xml: str | None = None,
    parent_label: str | None = None,
) -> AIResult:
    """Send *text* to the configured AI backend and return USLM XML + usage.

    The model returns a structured tree; this function serialises that tree
    into USLM XML. If the tree fails identifier-shape / parent-nesting /
    uniqueness checks, the model is re-prompted up to ``validation_retries``
    times with the violations attached.

    If *parent_xml* is provided, it is included as a reference block so the
    model can reuse stable `identifier` values from the prior version.
    """
    cfg = config or AIConfig.from_env()
    if not cfg.api_key:
        raise AIError(
            "OPENAI_API_KEY is not set. Configure it (or use a compatible "
            "backend via OPENAI_BASE_URL) before running the AI pipeline."
        )
    if not text.strip():
        raise AIError("Document text is empty; nothing to convert.")

    document = text
    if len(document) > cfg.max_input_chars:
        document = document[: cfg.max_input_chars]

    parent_block = ""
    if parent_xml:
        snippet = parent_xml
        if len(snippet) > cfg.max_parent_xml_chars:
            snippet = snippet[: cfg.max_parent_xml_chars]
        parent_block = PARENT_TEMPLATE.format(
            parent_label=parent_label or "previous", parent_xml=snippet
        )

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AIError("openai package is not installed") from exc

    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    user_msg = USER_TEMPLATE.format(
        code_id=code_id,
        version_label=version_label,
        parent_block=parent_block,
        document=document,
    )
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    total = AIUsage()
    parsed: USLMTree | None = None
    last_violations: list[str] = []
    attempts = max(1, cfg.validation_retries + 1)

    for attempt in range(attempts):
        response = client.chat.completions.parse(
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            response_format=USLMTree,
            messages=messages,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            total.prompt_tokens = (total.prompt_tokens or 0) + (
                getattr(usage, "prompt_tokens", 0) or 0
            )
            total.completion_tokens = (total.completion_tokens or 0) + (
                getattr(usage, "completion_tokens", 0) or 0
            )
            total.total_tokens = (total.total_tokens or 0) + (
                getattr(usage, "total_tokens", 0) or 0
            )
        candidate = response.choices[0].message.parsed
        if candidate is None:
            refusal = response.choices[0].message.refusal
            raise AIError(
                f"AI did not return structured USLM output (refusal: {refusal!r})"
                if refusal
                else "AI did not return structured USLM output."
            )
        last_violations = _validate_tree(candidate)
        if not last_violations:
            parsed = candidate
            break
        if attempt + 1 >= attempts:
            break
        messages = [
            *messages,
            {"role": "assistant", "content": candidate.model_dump_json()},
            {
                "role": "user",
                "content": (
                    "Your previous output had identifier issues. Return a "
                    "corrected tree, preserving all content and num/heading "
                    "verbatim. Fix every item below:\n- " + "\n- ".join(last_violations)
                ),
            },
        ]

    if parsed is None:
        raise AIError(
            "AI output failed structural validation after "
            f"{attempts} attempt(s): " + "; ".join(last_violations)
        )

    return AIResult(xml=_serialize_tree(parsed), usage=total)
