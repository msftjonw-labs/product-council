"""Render a council outcome as a decision memo in Markdown."""
from __future__ import annotations

from .roles import ROLES
from .schemas import CouncilOutcome


def _title(role: str) -> str:
    return ROLES[role].title if role in ROLES else role


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def render_markdown(o: CouncilOutcome) -> str:
    c, p = o.final_consensus, o.final_proposal
    verdict = "✅ CONSENSUS" if o.status == "consensus" else "⚠️ NO CONSENSUS"
    lines = [
        "# Product Council decision memo",
        "",
        f"**Question:** {o.question}",
        "",
        f"**Outcome:** {verdict} — {o.stop_reason}",
    ]
    if c:
        lines.append(f"**Final vote:** {c.agree}/{c.total} agree ({c.ratio:.0%}; threshold {c.threshold:.0%})")
    lines.append(f"**Iterations:** {len(o.iterations)} · **Evidence items:** {len(o.evidence)}")

    lines += [
        "", "## Requester-defined product brief", "",
        f"**Hypothesis:** {o.brief.hypothesis}", "",
        f"**Expected result:** {o.brief.expected_result}", "",
        "**Success metrics**", _bullets(o.brief.success_metrics), "",
        "These are requester-defined hypotheses and targets, not evidence of achieved outcomes.",
    ]

    if o.clarifications:
        lines += ["", "## Clarifications", ""]
        lines += [f"- **{q}** — {a}" for q, a in o.clarifications.items()]

    if p:
        lines += [
            "", f"## Proposal {p.id}: {p.title}", "",
            f"**Recommended decision:** `{p.decision}`", "", p.summary, "",
            "**Scope**", _bullets(p.scope), "", "**Out of scope**", _bullets(p.out_of_scope), "",
            "**Success metrics**", _bullets(p.success_metrics), "",
            "**Risks & mitigations**", _bullets(p.risks_and_mitigations), "",
            "**Open questions**", _bullets(p.open_questions), "",
            f"**Supporting evidence:** {', '.join(p.evidence_ids) or '(none)'}",
        ]

    last = o.iterations[-1] if o.iterations else None
    if last and last.votes:
        lines += ["", "## Final votes", "", "| Seat | Vote | Counted | Rationale | Evidence | Would change mind if |",
                  "|---|---|---|---|---|---|"]
        for v in last.votes:
            counted = "✅" if v.counts_as_agree else ("❌" if v.valid else f"❌ ({v.invalid_reason})")
            lines.append(f"| {_title(v.role)} | {v.vote} | {counted} | {_cell(v.rationale)} | "
                         f"{', '.join(v.evidence_ids)} | {_cell(v.what_would_change_my_mind)} |")

    lines += ["", "## Deliberation history", ""]
    for rec in o.iterations:
        cons = rec.consensus
        lines.append(f"### Iteration {rec.iteration}"
                     + (f" — {cons.agree}/{cons.total} ({cons.ratio:.0%})" if cons else ""))
        lines.append("")
        for s in rec.statements:
            lines.append(f"- **{_title(s.role)}** ({s.position}): {s.summary}")
        if rec.challenges:
            lines.append(f"- **Skeptic:** {rec.skeptic_summary}")
            for ch in rec.challenges:
                claim = next((c for c in o.claims if c.id == ch.claim_id), None)
                text = claim.text if claim else ""
                lines.append(f"  - {ch.claim_id} ({_title(ch.target_role)}, {ch.verdict} → {ch.resolution}): "
                             f"\"{_cell(text)}\" — {ch.reason}")
        if rec.research_requests:
            lines.append("- **Research commissioned:**")
            lines += [f"  - [{_title(r.requested_by)}] {r.query}" for r in rec.research_requests]
            lines.append(f"  - New evidence: {', '.join(rec.new_evidence_ids) or '(none found)'}")
        lines.append("")

    lines += ["## Evidence ledger", "", "| ID | Type | Title | Location | Iteration |", "|---|---|---|---|---|"]
    for e in o.evidence:
        lines.append(f"| {e.id} | {e.source_type} | {_cell(e.title)} | `{e.location}` | {e.retrieved_iteration} |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())
