"""AI-assisted conversion of plain text into United States Legislative Markup XML.

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

USLM_NAMESPACE = "http://schemas.gpo.gov/xml/uslm"

SYSTEM_PROMPT = """You convert raw legal/legislative documents into United States \
Legislative Markup (USLM) XML.

Output rules:
- Return a single XML document only. No prose, no markdown fences.
- Root element MUST be `<uslm xmlns="http://schemas.gpo.gov/xml/uslm">`.
- Include a `<meta>` block with `<dc:title>`, `<dc:type>`, and `<dc:identifier>` \
where `dc` is bound to `http://purl.org/dc/elements/1.1/`.
- Place the body inside `<main>`.
- Use hierarchical USLM elements as appropriate: `title`, `subtitle`, `chapter`, \
`subchapter`, `part`, `subpart`, `section`, `subsection`, `paragraph`, \
`subparagraph`, `clause`, `subclause`. Each container should have `<num>` and \
`<heading>` children where present in the source, and a `<content>` child \
holding `<p>` elements with the actual text.
- Preserve the original numbering and headings exactly as they appear.
- Do not invent content. If unsure of a heading, omit it.
- Keep text faithful to the source; do not summarize or paraphrase.
- Ensure the document is well-formed XML.
"""

USER_TEMPLATE = """Convert the following document into USLM XML.

code-id: {code_id}
version: {version_label}

--- BEGIN DOCUMENT ---
{document}
--- END DOCUMENT ---
"""


@dataclass
class AIConfig:
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-5.4-mini"
    temperature: float = 0.0
    max_input_chars: int = 200_000

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


def generate_uslm_xml(
    text: str,
    *,
    code_id: str,
    version_label: str,
    config: AIConfig | None = None,
) -> str:
    """Send *text* to the configured AI backend and return USLM XML."""
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

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AIError("openai package is not installed") from exc

    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    response = client.chat.completions.create(
        model=cfg.model,
        temperature=cfg.temperature,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    code_id=code_id,
                    version_label=version_label,
                    document=document,
                ),
            },
        ],
    )
    raw = (response.choices[0].message.content or "").strip()
    return _strip_code_fences(raw)


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    return text
