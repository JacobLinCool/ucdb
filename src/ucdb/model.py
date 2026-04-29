"""Canonical in-memory legal document model for UCDB 0.2."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LegalNode:
    """One structural legal node in document order."""

    node_type: str
    eId: str
    num: str | None = None
    heading: str | None = None
    paragraphs: list[str] = field(default_factory=list)
    children: list["LegalNode"] = field(default_factory=list)
    profile_type: str | None = None
    source_locator: str | None = None


@dataclass
class LegalDocument:
    """Normalized document emitted by importers and serialized as Akoma Ntoso."""

    work_id: str
    version_label: str
    title: str | None = None
    jurisdiction: str = "tw"
    document_class: str = "law"
    language: str = "zho"
    expression_date: str | None = None
    effective_date: str | None = None
    promulgation_date: str | None = None
    enforcement_date: str | None = None
    source_authority: str | None = None
    source_url: str | None = None
    nodes: list[LegalNode] = field(default_factory=list)

    @property
    def work_uri(self) -> str:
        return f"/akn/{self.jurisdiction}/act/{self.document_class}/{self.work_id}"

    @property
    def expression_uri(self) -> str:
        date = self.expression_date or self.version_label
        return f"{self.work_uri}/{self.language}@{date}"
