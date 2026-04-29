"""Taiwan LegalDocML profile helpers."""

from __future__ import annotations

import re
import unicodedata

AKN_PROFILE = "tw-legaldocml@0.2"

NODE_TYPE_ALIASES = {
    "編": "part",
    "章": "chapter",
    "節": "section",
    "條": "article",
    "項": "paragraph",
    "款": "point",
    "目": "indent",
    "附件": "attachment",
    "附表": "schedule",
    "附錄": "appendix",
}

_CJK_NUMERALS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "百": 100,
    "千": 1000,
}

_ARTICLE_RE = re.compile(
    r"第([零〇一二三四五六七八九十百千0-9]+)條(?:之([零〇一二三四五六七八九十百千0-9]+))?"
)
_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")


def normalize_eid_token(value: str) -> str:
    """Return an ASCII-safe Akoma Ntoso ``eId`` token."""
    value = unicodedata.normalize("NFKC", value).strip()
    match = _ARTICLE_RE.search(value)
    if match:
        base = _parse_number(match.group(1))
        suffix = match.group(2)
        if suffix:
            return f"art_{base}_{_parse_number(suffix)}"
        return f"art_{base}"
    value = value.lower().replace("-", "_").replace("/", "_")
    value = _SAFE_RE.sub("_", value).strip("_")
    return value or "node"


def stable_node_eid(node_type: str, num: str | None, ordinal: int) -> str:
    prefix = {
        "part": "part",
        "chapter": "chp",
        "section": "sec",
        "article": "art",
        "paragraph": "para",
        "point": "point",
        "indent": "indent",
        "attachment": "att",
        "schedule": "sch",
        "appendix": "app",
    }.get(node_type, "node")
    if num:
        token = normalize_eid_token(num)
        if token.startswith(prefix + "_"):
            return token
        return f"{prefix}_{token}"
    return f"{prefix}_{ordinal}"


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _parse_number(value: str) -> int:
    value = unicodedata.normalize("NFKC", value)
    if value.isdigit():
        return int(value)
    total = 0
    section = 0
    number = 0
    for char in value:
        n = _CJK_NUMERALS.get(char)
        if n is None:
            continue
        if n >= 10:
            section += (number or 1) * n
            number = 0
        else:
            number = n
    total += section + number
    return total
