"""Deterministic offline provider used for demos and tests (no API keys needed).

It simulates each seat's behaviour from the structured metadata the orchestrator attaches to every
request: a seat agrees only once the ledger contains evidence addressing its core concern; otherwise it
disagrees and requests targeted research. This exercises the full clarify -> debate -> challenge ->
vote -> research loop.
"""
from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider, LLMRequest

# Per-seat concern: keywords that must appear in ledger evidence, plus the research it asks for.
CONCERNS: dict[str, dict[str, Any]] = {
    "customer_success": {
        "keywords": ["renewal", "retention"],
        "query": "customer retention renewal risk after migration",
        "source_types": ["customer_interview"],
        "question": "How many customers or field reports describe the post-migration gap, and in which segments?",
    },
    "engineering": {
        "keywords": ["engineering effort", "reuse"],
        "query": "engineering effort estimate reuse existing advisor monitoring components",
        "source_types": ["internal_doc"],
        "question": "Which existing services (e.g. advisory, monitoring) could be reused rather than built new?",
    },
    "product": {
        "keywords": ["vision", "roadmap"],
        "query": "team vision roadmap end-to-end modernization journey operate",
        "source_types": ["team_vision", "company_vision"],
        "question": "Is 'operate / day-2' explicitly within the team's charter and roadmap?",
    },
    "marketing": {
        "keywords": ["competitor", "competitive"],
        "query": "competitor competitive landscape post-migration managed operations offers",
        "source_types": ["internal_doc", "partnership"],
        "question": "What do competitors offer for post-migration operations today?",
    },
    "security": {
        "keywords": ["misconfiguration", "secure score"],
        "query": "security posture misconfiguration secure score after migration",
        "source_types": ["internal_doc"],
        "question": "Is there data on security posture drift in the first months after migration?",
    },
    "support": {
        "keywords": ["case volume", "support cases"],
        "query": "support case volume drivers first 90 days after migration",
        "source_types": ["internal_doc"],
        "question": "What share of post-migration support cases are operational how-to vs. product defects?",
    },
    "forecast": {
        "keywords": ["case volume", "adoption"],
        "query": "post-release adoption support case volume first 90 days operational workload",
        "source_types": ["internal_doc", "customer_interview"],
        "question": "What adoption, operational workload, and follow-up activities are expected in the first 90 days?",
    },
}


def _matching(evidence: list[dict], keywords: list[str]) -> list[str]:
    return [e["id"] for e in evidence if any(k in e["excerpt"].lower() for k in keywords)]


class MockProvider(LLMProvider):
    name = "mock"

    async def complete(self, request: LLMRequest) -> str:
        md = request.metadata
        handler = getattr(self, f"_{md.get('task')}", None)
        if handler is None:
            raise ValueError(f"MockProvider has no handler for task {md.get('task')!r}")
        return json.dumps(handler(md))

    def _clarify(self, md: dict) -> dict:
        concern = CONCERNS.get(md["role"])
        if concern is None:  # skeptic
            return {"questions": ["What is the source and sample size behind the field feedback?"]}
        return {"questions": [concern["question"]]}

    def _consolidate_questions(self, md: dict) -> dict:
        seen, out = set(), []
        for q in md["candidates"]:
            if q["question"] not in seen:
                seen.add(q["question"])
                out.append(q["question"])
        return {"questions": out[: md["max_questions"]]}

    def _statement(self, md: dict) -> dict:
        role, evidence = md["role"], md["evidence"]
        concern = CONCERNS[role]
        hits = _matching(evidence, concern["keywords"])
        general = [e["id"] for e in evidence if e["source_type"] in concern["source_types"]][:1]
        claims = []
        if hits:
            claims.append({"text": f"Evidence addresses the {role} concern directly.", "evidence_ids": hits[:2]})
        elif general:
            claims.append({"text": f"Related {role} evidence exists but does not settle the concern.",
                           "evidence_ids": general})
        claims.append({"text": f"From the {role} lens, impact is likely significant.", "evidence_ids": [],
                       "is_assumption": not hits})
        summary = f"{role} {'supports' if hits else 'needs more data before supporting'} the investment."
        if role == "forecast":
            summary += (
                " Assumed post-release activities (not proven outcomes): days 1-30, Customer Success leads "
                "onboarding and Engineering monitors reliability; days 31-60, Support reviews case drivers "
                "and Engineering adjusts capacity; days 61-90, Product reviews customer feedback and the "
                "requester's success metrics to prioritise follow-up releases. Dependencies: telemetry and "
                "staffing. Low adoption or higher support load would require revising this outlook."
            )
        return {
            "position": "support" if hits else "conditional",
            "summary": summary,
            "claims": claims,
            "research_requests": [] if hits else [
                {"query": concern["query"], "source_types": concern["source_types"],
                 "rationale": f"{role} concern not yet evidenced"}
            ],
        }

    def _skeptic_review(self, md: dict) -> dict:
        verdicts, requests = [], []
        for c in md["claims"]:
            if not c["evidence_ids"] or c["invalid_evidence_ids"]:
                verdict, reason = "unsupported", "No valid evidence cited."
            elif c["is_assumption"]:
                verdict, reason = "weak", "Self-declared assumption."
            else:
                verdict, reason = "supported", "Cited evidence matches the claim."
            verdicts.append({"claim_id": c["id"], "verdict": verdict, "reason": reason,
                             "required_evidence": "" if verdict == "supported" else "Quantified data for this claim."})
            if verdict != "supported" and c["role"] in CONCERNS:
                con = CONCERNS[c["role"]]
                requests.append({"query": con["query"], "source_types": con["source_types"],
                                 "rationale": f"Verify {c['id']}"})
        n_bad = sum(v["verdict"] != "supported" for v in verdicts)
        return {"verdicts": verdicts, "research_requests": requests,
                "summary": f"{n_bad} of {len(verdicts)} claims are weak or unsupported."}

    def _rebuttal(self, md: dict) -> dict:
        return {"responses": [{"claim_id": ch["claim_id"], "action": "withdraw", "text": "", "evidence_ids": []}
                              for ch in md["challenges"]], "research_requests": []}

    def _proposal(self, md: dict) -> dict:
        evidence = md["evidence"]
        prev = md.get("previous_proposal")
        return {
            "title": "Post-migration (Day-2) guidance & recommendations experience",
            "decision": "pilot",
            "summary": "Pilot a guided day-2 operations experience for migrated on-premises workloads, reusing "
                       "existing advisory and monitoring signals, with partner integration for managed operations.",
            "scope": ["Post-cutover health & posture baseline", "Prioritised recommendations", "Guided runbooks"],
            "out_of_scope": ["Fully managed operations service"],
            "success_metrics": ["Reduction in post-migration support cases", "Secure score improvement at day 90"],
            "risks_and_mitigations": ["Overlap with partner offers -> integrate rather than duplicate"],
            "open_questions": [],
            "evidence_ids": [e["id"] for e in evidence[:6]],
            "changes_from_previous": "Addressed dissent with newly researched evidence." if prev else "",
        }

    def _vote(self, md: dict) -> dict:
        role, evidence = md["role"], md["evidence"]
        if role == "skeptic":
            types = {e["source_type"] for e in evidence}
            claims = md["iteration_claims"]
            ok = [c for c in claims if c["status"] in ("supported", "defended")]
            all_concerns = all(_matching(evidence, c["keywords"]) for c in CONCERNS.values())
            agree = all_concerns and len(types) >= 3 and bool(ok)
            return {"vote": "agree" if agree else "disagree",
                    "rationale": f"{len(ok)}/{len(claims)} claims supported across {len(types)} source types.",
                    "evidence_ids": [e["id"] for e in evidence[:3]],
                    "what_would_change_my_mind": "" if agree else "Evidence for every seat's core concern.",
                    "research_requests": []}
        concern = CONCERNS[role]
        hits = _matching(evidence, concern["keywords"])
        if hits:
            return {"vote": "agree", "rationale": f"{role} concern is evidenced.", "evidence_ids": hits[:2],
                    "conditions": [], "what_would_change_my_mind": "", "research_requests": []}
        return {"vote": "disagree", "rationale": f"No evidence yet for the {role} concern.", "evidence_ids": [],
                "conditions": [], "what_would_change_my_mind": concern["query"],
                "research_requests": [{"query": concern["query"], "source_types": concern["source_types"],
                                       "rationale": "Needed to change vote"}]}
