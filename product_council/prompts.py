"""Prompt templates and JSON response schemas for each council task."""
from __future__ import annotations

import json
from typing import Any

from .schemas import DECISIONS, Evidence

RESEARCH_REQUEST = ('{"query": str, "source_types": [one or more of internal_doc|customer_interview|team_vision|'
                    'company_vision|partnership|user_provided], "rationale": str}')

SCHEMAS = {
    "clarify": '{"questions": [str]}  // at most 2 questions, only ones whose answer would change the decision',
    "consolidate_questions": '{"questions": [str]}  // deduplicated, highest value first',
    "statement": ('{"position": "support"|"oppose"|"conditional", "summary": str, "claims": [{"text": str, '
                  '"evidence_ids": [str], "is_assumption": bool}], "research_requests": [' + RESEARCH_REQUEST + ']}'),
    "skeptic_review": ('{"verdicts": [{"claim_id": str, "verdict": "supported"|"weak"|"unsupported", "reason": str, '
                       '"required_evidence": str}], "research_requests": [' + RESEARCH_REQUEST + '], "summary": str}'),
    "rebuttal": ('{"responses": [{"claim_id": str, "action": "defend"|"revise"|"withdraw", "text": str, '
                 '"evidence_ids": [str]}], "research_requests": [' + RESEARCH_REQUEST + ']}'),
    "proposal": ('{"title": str, "decision": ' + "|".join(f'"{d}"' for d in DECISIONS) + ', "summary": str, '
                 '"scope": [str], "out_of_scope": [str], "success_metrics": [str], "risks_and_mitigations": [str], '
                 '"open_questions": [str], "evidence_ids": [str], "changes_from_previous": str}'),
    "vote": ('{"vote": "agree"|"disagree", "rationale": str, "evidence_ids": [str], "conditions": [str], '
             '"what_would_change_my_mind": str, "research_requests": [' + RESEARCH_REQUEST + ']}'),
}

INSTRUCTIONS = {
    "clarify": "Before the debate starts, list the clarifying questions you need answered by the requester.",
    "consolidate_questions": "Merge these candidate clarifying questions into at most {max_questions} "
                             "non-overlapping questions, ordered by how much the answer could change the decision.",
    "statement": "State your position on the question from your seat's perspective. Ground every claim in the "
                 "ledger. Evaluate the requester's hypothesis, expected result, and success metrics as targets, "
                 "not established facts. If a previous round exists, respond to dissent and the Skeptic's challenges.",
    "skeptic_review": "Audit every claim below. Check that each cited evidence ID exists and actually supports the "
                      "claim. Request research for anything weak or unsupported.",
    "rebuttal": "The Skeptic challenged the claims below. For each: defend it with (additional) valid evidence IDs, "
                "revise it to what the evidence supports, or withdraw it. Request research if needed.",
    "proposal": "Synthesise ONE proposal the council will vote on. Use only supported/defended claims as premises. "
                "Retain the requester-defined success metrics; add proposal-specific metrics if needed. Include "
                "post-release activities and risks from the Forecast Agent. "
                "If a previous proposal failed, address the dissenters' conditions where evidence permits.",
    "vote": "Vote on the proposal. 'agree' requires at least one valid evidence ID supporting your rationale. "
            "If you disagree, say precisely what evidence would change your mind and request that research.",
}


def render_ledger(evidence: list[Evidence]) -> str:
    if not evidence:
        return "(empty - no evidence collected yet)"
    return "\n\n".join(e.brief() for e in evidence)


def build_prompt(task: str, *, question: str, clarifications: dict[str, str], evidence: list[Evidence],
                 sections: dict[str, Any] | None = None, **fmt: Any) -> str:
    parts = [f"# Question under review\n{question}"]
    if clarifications:
        parts.append("# Clarifications from the requester\n" +
                     "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items()))
    parts.append(f"# Evidence Ledger (cite by ID)\n{render_ledger(evidence)}")
    for title, body in (sections or {}).items():
        text = body if isinstance(body, str) else json.dumps(body, indent=2, default=str)
        parts.append(f"# {title}\n{text}")
    parts.append(f"# Task\n{INSTRUCTIONS[task].format(**fmt)}")
    parts.append(f"# Response JSON schema\n{SCHEMAS[task]}")
    return "\n\n".join(parts)


def parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model response")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Model response JSON is not an object")
    return data
