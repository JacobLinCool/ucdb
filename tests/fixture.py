"""Synthetic legal-code fixture used by the diff/blame test suite.

We construct a fictional ``tax-code`` with ten sections (~100 lines total at
v1) and emit ten successive Akoma Ntoso XML snapshots that exercise every kind of
edit the pipeline must track:

* Pure additions of brand-new sections.
* Removal of an entire section, then re-introduction many versions later
  (which must reset its blame to the re-introduction version).
* Multiple modifications to the same section across the timeline so blame on
  surviving lines points to v1 while edited lines point to the editing
  version.
* A small wording fix and a structural rewrite within the same section.

The fixture is deterministic and depends only on the standard library, so it
can run inside ``uv run pytest`` or as a plain ``python tests/...`` script.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class Section:
    eid: str
    num: str
    heading: str
    lines: list[str] = field(default_factory=list)


# v1 — initial 10 sections, ~100 content lines.
V1: list[Section] = [
    Section(
        "art_1",
        "1",
        "Definitions",
        [
            "In this Code, the following terms have the meanings given below.",
            "Income means all gains, receipts, and accretions to wealth.",
            "Taxpayer means any person subject to the provisions of this Code.",
            "Year means the calendar year unless context indicates otherwise.",
            "Resident means a person domiciled in the jurisdiction.",
            "Non-resident means a person not domiciled in the jurisdiction.",
            "Spouse means a person legally married to the taxpayer.",
            "Dependent means a person whose support is supplied by the taxpayer.",
            "Charity means an organization recognized as exempt under section 5.",
            "Authority means the Department of Revenue and its agents.",
        ],
    ),
    Section(
        "art_2",
        "2",
        "Filing Requirements",
        [
            "Every resident with income above the filing threshold shall file annually.",
            "The filing deadline is the fifteenth day of April of the following year.",
            "Returns shall be filed in the form prescribed by the Authority.",
            "Joint returns are permitted for spouses living together at year end.",
            "Extensions of time may be granted for good cause shown.",
            "Late filings incur a penalty of five percent per month, capped at twenty-five percent.",
            "Electronic filing is mandatory for taxpayers with income above one hundred thousand.",
            "Paper returns shall be signed and dated by the taxpayer.",
            "Amended returns may be filed within three years of the original deadline.",
            "Supporting documents shall be retained as required by section 10.",
        ],
    ),
    Section(
        "art_3",
        "3",
        "Tax Rates",
        [
            "The standard rate of tax is twenty-five percent of taxable income.",
            "A reduced rate of fifteen percent applies to long-term capital gains.",
            "An additional surtax of three percent applies to income above one million.",
            "Different rates apply to non-residents as set out in subsection (d).",
            "Indexed thresholds shall be adjusted annually for inflation.",
            "The Authority may issue tables to assist taxpayers in computing tax.",
            "Tax shall be rounded to the nearest whole unit of currency.",
            "No fractional tax shall be assessed below one unit of currency.",
            "Negative taxable income produces zero tax for the year.",
            "Carryforward of losses is governed by section 4.",
        ],
    ),
    Section(
        "art_4",
        "4",
        "Deductions",
        [
            "Taxpayers may deduct ordinary and necessary business expenses.",
            "Charitable contributions are deductible up to thirty percent of income.",
            "Mortgage interest on a primary residence is deductible.",
            "State and local taxes are deductible up to ten thousand per return.",
            "Medical expenses are deductible to the extent they exceed seven percent of income.",
            "Casualty losses are deductible only in federally declared disaster areas.",
            "Net operating losses may be carried forward five years.",
            "Capital losses offset capital gains and up to three thousand of ordinary income.",
            "Above-the-line deductions are listed in the prescribed schedule.",
            "All deductions must be substantiated by contemporaneous records.",
        ],
    ),
    Section(
        "art_5",
        "5",
        "Credits",
        [
            "A credit of two thousand is allowed for each qualifying dependent.",
            "An earned income credit is available to lower-income workers.",
            "A credit for taxes paid to other jurisdictions prevents double taxation.",
            "Energy efficiency improvements qualify for a credit of up to one thousand.",
            "Adoption expenses qualify for a credit of up to fourteen thousand.",
            "Credits are non-refundable except where this Code explicitly provides otherwise.",
            "Unused credits expire at the end of the tax year unless carryforward is allowed.",
            "Credits are claimed on the schedule prescribed by the Authority.",
            "Fraudulent claims are punishable under section 6.",
            "The Authority may issue regulations clarifying eligibility.",
        ],
    ),
    Section(
        "art_6",
        "6",
        "Penalties",
        [
            "Failure to file a required return is a civil violation.",
            "Failure to pay tax when due incurs interest at the prevailing rate.",
            "Negligent understatement of tax incurs a twenty percent penalty.",
            "Fraudulent understatement incurs a seventy-five percent penalty.",
            "Willful failure to file is a criminal misdemeanor.",
            "Willful evasion of tax is a felony punishable by imprisonment.",
            "Penalties may be abated for reasonable cause.",
            "First-time abatement is available for taxpayers in good standing.",
            "Penalties accrue separately from interest on the unpaid amount.",
            "The Authority shall publish penalty amounts annually.",
        ],
    ),
    Section(
        "art_7",
        "7",
        "Audits",
        [
            "The Authority may audit any return within three years of filing.",
            "The look-back period extends to six years for substantial understatements.",
            "There is no statute of limitations for fraudulent returns.",
            "Taxpayers must produce records on reasonable notice.",
            "Audits may be conducted by mail, in person, or at a field office.",
            "The taxpayer is entitled to representation throughout an audit.",
            "Audit findings must be communicated in writing.",
            "Disputes are resolved through the appeals process in section 8.",
            "The Authority may assess additional tax based on audit findings.",
            "Statistical sampling is permitted with the taxpayer's consent.",
        ],
    ),
    Section(
        "art_8",
        "8",
        "Appeals",
        [
            "A taxpayer aggrieved by an assessment may appeal within ninety days.",
            "Appeals are heard by an independent administrative tribunal.",
            "The taxpayer bears the burden of proof on contested factual issues.",
            "Decisions of the tribunal may be appealed to the courts.",
            "Filing an appeal does not stay collection unless ordered by the tribunal.",
            "Settlement offers may be made at any stage of the appeal.",
            "The tribunal shall issue written decisions within one hundred eighty days.",
            "Precedential decisions shall be published by the Authority.",
            "Costs may be awarded against the losing party in exceptional cases.",
            "Tribunal procedures are governed by separate regulations.",
        ],
    ),
    Section(
        "art_9",
        "9",
        "Refunds",
        [
            "Overpaid tax is refundable upon proper claim.",
            "Refund claims must be filed within three years of the original return.",
            "Refunds may be applied to the following year's tax at the taxpayer's election.",
            "Interest accrues on refunds beginning forty-five days after the claim.",
            "The Authority may offset refunds against other government debts.",
            "Refund checks are mailed unless direct deposit is elected.",
            "Refunds smaller than one currency unit are not issued.",
            "Erroneous refunds may be reclaimed within two years.",
            "Refund processing is suspended during fraud review.",
            "Statistics on refund processing times are published quarterly.",
        ],
    ),
    Section(
        "art_10",
        "10",
        "Records Retention",
        [
            "Taxpayers shall retain records supporting their returns for six years.",
            "Records relating to property must be kept while owned plus six years.",
            "Electronic records are acceptable if they preserve original detail.",
            "The Authority may inspect records during reasonable business hours.",
            "Records destroyed in a casualty event must be reconstructed in good faith.",
            "Failure to maintain records may result in disallowance of deductions.",
            "The Authority publishes guidance on acceptable record formats.",
            "Foreign-language records must be accompanied by certified translations.",
            "Privileged materials are protected as set out in subsection (c).",
            "These rules apply to both individuals and business entities.",
        ],
    ),
]


def _clone(sections: list[Section]) -> list[Section]:
    return [deepcopy(s) for s in sections]


def _replace_line(s: Section, idx: int, new_text: str) -> None:
    s.lines[idx] = new_text


def build_versions() -> list[tuple[str, list[Section]]]:
    """Return ten ``(version_label, sections)`` snapshots in chronological order."""
    versions: list[tuple[str, list[Section]]] = []

    # v1 — initial.
    versions.append(("2020-01-01", _clone(V1)))

    # v2 — small wording fix in s1; add a new privacy section s11.
    s = _clone(V1)
    _replace_line(
        s[0],
        1,
        "Income means all gains, receipts, accretions, and other accessions to wealth.",
    )
    s.append(
        Section(
            "art_11",
            "11",
            "Privacy of Returns",
            [
                "Tax returns and return information are confidential.",
                "Disclosure is permitted only as authorized by law.",
                "Unauthorized disclosure is punishable as a criminal offense.",
                "Aggregated statistical data may be published.",
                "The Authority shall maintain reasonable safeguards.",
            ],
        )
    )
    versions.append(("2020-07-01", s))

    # v3 — raise the standard rate from 25% to 28%; raise the surtax to 4%.
    s = _clone(versions[-1][1])
    _replace_line(
        s[2],
        0,
        "The standard rate of tax is twenty-eight percent of taxable income.",
    )
    _replace_line(
        s[2],
        2,
        "An additional surtax of four percent applies to income above one million.",
    )
    versions.append(("2021-01-01", s))

    # v4 — repeal the credits section entirely.
    s = _clone(versions[-1][1])
    s = [sec for sec in s if sec.eid != "art_5"]
    versions.append(("2021-07-01", s))

    # v5 — modernize filing (s2) and tighten the SALT cap (s4).
    s = _clone(versions[-1][1])
    s2 = next(sec for sec in s if sec.eid == "art_2")
    _replace_line(
        s2,
        1,
        "The filing deadline is the fifteenth day of April or the next business day.",
    )
    _replace_line(
        s2,
        6,
        "Electronic filing is mandatory for taxpayers with income above fifty thousand.",
    )
    _replace_line(
        s2,
        8,
        "Amended returns may be filed within four years of the original deadline.",
    )
    s4 = next(sec for sec in s if sec.eid == "art_4")
    _replace_line(
        s4, 3, "State and local taxes are deductible up to five thousand per return."
    )
    versions.append(("2022-01-01", s))

    # v6 — add an international transactions section; tighten audit notice.
    s = _clone(versions[-1][1])
    s.append(
        Section(
            "art_12",
            "12",
            "International Transactions",
            [
                "Cross-border income is taxable to the extent provided in this section.",
                "Tax treaties prevail over conflicting provisions of this Code.",
                "Foreign tax paid is creditable subject to the limitations in section 4.",
                "Reportable foreign accounts must be disclosed annually.",
                "Transfer pricing is governed by separate regulations.",
            ],
        )
    )
    s7 = next(sec for sec in s if sec.eid == "art_7")
    _replace_line(
        s7, 3, "Taxpayers must produce records within thirty days of a written request."
    )
    versions.append(("2022-07-01", s))

    # v7 — re-introduce a rewritten Credits section; adjust privacy wording.
    s = _clone(versions[-1][1])
    s.insert(
        4,
        Section(
            "art_5",
            "5",
            "Credits",
            [
                "A refundable credit of three thousand is allowed for each qualifying child.",
                "A working family credit replaces the prior earned income credit.",
                "A credit for clean energy investments equals thirty percent of qualifying spending.",
                "Foreign tax credits remain available subject to section 12.",
                "All credits in this section are claimed on a unified schedule.",
                "Eligibility is determined as of the last day of the tax year.",
            ],
        ),
    )
    s11 = next(sec for sec in s if sec.eid == "art_11")
    _replace_line(
        s11,
        2,
        "Unauthorized disclosure is punishable by fine and imprisonment.",
    )
    versions.append(("2023-01-01", s))

    # v8 — expand definitions; trim retention.
    s = _clone(versions[-1][1])
    s1 = next(sec for sec in s if sec.eid == "art_1")
    s1.lines.extend(
        [
            "Digital asset means a cryptographically secured representation of value.",
            "Pass-through entity means a partnership, S corporation, or similar arrangement.",
        ]
    )
    s10 = next(sec for sec in s if sec.eid == "art_10")
    s10.lines = s10.lines[:7]
    versions.append(("2023-07-01", s))

    # v9 — repeal international section; tighten appeal procedure.
    s = _clone(versions[-1][1])
    s = [sec for sec in s if sec.eid != "art_12"]
    s8 = next(sec for sec in s if sec.eid == "art_8")
    _replace_line(
        s8, 4, "Filing an appeal automatically stays collection for sixty days."
    )
    versions.append(("2024-01-01", s))

    # v10 — return the standard rate to 25%; small wording fix in s6 and s9.
    s = _clone(versions[-1][1])
    s3 = next(sec for sec in s if sec.eid == "art_3")
    _replace_line(
        s3, 0, "The standard rate of tax is twenty-five percent of taxable income."
    )
    s6 = next(sec for sec in s if sec.eid == "art_6")
    _replace_line(
        s6, 6, "Penalties may be abated for reasonable cause as defined by regulation."
    )
    s9 = next(sec for sec in s if sec.eid == "art_9")
    _replace_line(
        s9,
        4,
        "The Authority may offset refunds against any outstanding government debt.",
    )
    versions.append(("2024-07-01", s))

    assert len(versions) == 10
    return versions


def to_akn_xml(version_label: str, sections: list[Section]) -> str:
    """Render a list of sections into a minimal-but-valid Akoma Ntoso XML document."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">',
        '  <act name="law" contains="originalVersion" ucdbProfile="tw-legaldocml@0.2">',
        "    <meta>",
        '      <identification source="#ucdb">',
        "        <FRBRWork>",
        '          <FRBRthis value="/akn/tw/act/law/tax-code/!main"/>',
        '          <FRBRuri value="/akn/tw/act/law/tax-code"/>',
        f'          <FRBRdate date="{version_label}" name="Made-Up Tax Code"/>',
        '          <FRBRauthor href="#source" as="#author"/>',
        '          <FRBRcountry value="tw"/>',
        '          <FRBRsubtype value="law"/>',
        '          <FRBRname value="Made-Up Tax Code"/>',
        "        </FRBRWork>",
        "        <FRBRExpression>",
        f'          <FRBRthis value="/akn/tw/act/law/tax-code/zho@{version_label}/!main"/>',
        f'          <FRBRuri value="/akn/tw/act/law/tax-code/zho@{version_label}"/>',
        f'          <FRBRdate date="{version_label}" name="{version_label}"/>',
        '          <FRBRauthor href="#ucdb" as="#editor"/>',
        '          <FRBRlanguage language="zho"/>',
        "        </FRBRExpression>",
        "        <FRBRManifestation>",
        f'          <FRBRthis value="/akn/tw/act/law/tax-code/zho@{version_label}/!main.xml"/>',
        f'          <FRBRuri value="/akn/tw/act/law/tax-code/zho@{version_label}.akn"/>',
        f'          <FRBRdate date="{version_label}" name="UCDB Akoma Ntoso XML"/>',
        '          <FRBRauthor href="#ucdb" as="#generator"/>',
        "        </FRBRManifestation>",
        "      </identification>",
        '      <references source="#ucdb">',
        '        <TLCOrganization eId="ucdb" href="/akn/tw/ontology/organization/ucdb" showAs="Universal Code Database"/>',
        '        <TLCOrganization eId="source" href="/akn/tw/source/test" showAs="Fixture"/>',
        "      </references>",
        "    </meta>",
        "    <body>",
        '      <part eId="part_1">',
        "        <num>I</num>",
        "        <heading>Tax Code</heading>",
    ]
    for sec in sections:
        parts.append(f'        <article eId="{sec.eid}">')
        parts.append(f"          <num>{sec.num}</num>")
        parts.append(f"          <heading>{_xml_escape(sec.heading)}</heading>")
        parts.append("          <content>")
        for line in sec.lines:
            parts.append(f"            <p>{_xml_escape(line)}</p>")
        parts.append("          </content>")
        parts.append("        </article>")
    parts.append("      </part>")
    parts.append("    </body>")
    parts.append("  </act>")
    parts.append("</akomaNtoso>")
    return "\n".join(parts)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    # Quick sanity check when run directly.
    versions = build_versions()
    line_counts = [sum(len(sec.lines) for sec in secs) for _, secs in versions]
    print("snapshot line counts:", line_counts)
    print("v1 line count:", line_counts[0])
