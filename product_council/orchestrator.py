"""Product Council orchestrator: clarify -> research -> (debate -> challenge -> rebut -> propose -> vote ->
research)* until the consensus threshold is met or the loop is exhausted."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from .config import CouncilConfig, SeatConfig
from .prompts import build_prompt, parse_json
from .providers import LLMProvider, LLMRequest, create_provider
from .research import EvidenceLedger, HumanSource, LocalCorpusSource, ResearchEngine, ResearchSource
from .roles import DEBATING_ROLES, MODERATOR_PROMPT, ROLES
from .schemas import (
    DECISIONS, SOURCE_TYPES, Challenge, Claim, ConsensusResult, CouncilOutcome, IterationRecord, Proposal,
    ProductBrief, ResearchRequest, Statement, Vote,
)

log = logging.getLogger("product_council")

ClarifyFn = Callable[[list[str]], Awaitable[dict[str, str]]]
EventFn = Callable[[str, dict[str, Any]], None]


class AgentError(RuntimeError):
    pass


def tally(votes: list[Vote], proposal_id: str, threshold: float) -> ConsensusResult:
    return ConsensusResult(proposal_id, sum(v.counts_as_agree for v in votes), len(votes), threshold)


def build_sources(cfg: CouncilConfig, ask_human: Callable[[str], Awaitable[str]] | None = None) -> list[ResearchSource]:
    sources: list[ResearchSource] = []
    for spec in cfg.sources:
        if spec.get("type", "local") == "local":
            sources.append(LocalCorpusSource(spec["path"], spec["source_type"]))
        else:
            raise ValueError(f"Unknown source type: {spec.get('type')}")
    if ask_human:
        sources.append(HumanSource(ask_human))
    return sources


class Council:
    def __init__(
        self,
        cfg: CouncilConfig,
        *,
        providers: dict[str, LLMProvider] | None = None,
        sources: list[ResearchSource] | None = None,
        clarify: ClarifyFn | None = None,
        on_event: EventFn | None = None,
        retry_backoff: float = 1.0,
    ) -> None:
        cfg.validate()
        self.cfg = cfg
        if providers is None:
            used = {s.provider for s in [*cfg.seats.values(), cfg.moderator]}
            providers = {name: create_provider(cfg.providers[name]) for name in sorted(used)}
        self.providers = providers
        self.ledger = EvidenceLedger()
        self.research = ResearchEngine(sources if sources is not None else build_sources(cfg), self.ledger,
                                       cfg.research_top_k)
        self.clarify_fn = clarify
        self.emit = on_event or (lambda kind, payload: None)
        self.retry_backoff = retry_backoff
        self.claims: dict[str, Claim] = {}
        self.question = ""
        self.clarifications: dict[str, str] = {}
        self.brief: ProductBrief | None = None

    @property
    def voters(self) -> list[str]:
        return DEBATING_ROLES + (["skeptic"] if self.cfg.skeptic_votes else [])

    # ------------------------------------------------------------------ model calls
    async def _call(self, seat_key: str, task: str, user: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if seat_key == "moderator":
            seat, system = self.cfg.moderator, MODERATOR_PROMPT
        else:
            seat, system = self.cfg.seats[seat_key], ROLES[seat_key].system_prompt()
        provider = self.providers[seat.provider]
        md = {"task": task, "role": seat_key, **metadata,
              "brief": asdict(self.brief) if self.brief else None,
              "evidence": [asdict(e) for e in self.ledger.all()]}
        last: Exception | None = None
        prompt = user
        for attempt in range(3):
            try:
                raw = await provider.complete(self._request(seat, system, prompt, md))
                return parse_json(raw)
            except Exception as exc:  # noqa: BLE001 - surface any provider/parse failure uniformly
                last = exc
                log.warning("%s/%s attempt %d failed: %s", seat_key, task, attempt + 1, exc)
                if isinstance(exc, ValueError):
                    prompt = user + "\n\nYour previous reply was not a valid JSON object. Reply with JSON only."
                await asyncio.sleep(self.retry_backoff * 2**attempt)
        raise AgentError(f"{seat_key} failed task {task}: {last}")

    @staticmethod
    def _request(seat: SeatConfig, system: str, user: str, md: dict[str, Any]) -> LLMRequest:
        return LLMRequest(model=seat.model, system=system, user=user, temperature=seat.temperature,
                          max_tokens=seat.max_tokens, metadata=md)

    def _prompt(self, task: str, sections: dict[str, Any] | None = None, **fmt: Any) -> str:
        context: dict[str, Any] = {}
        if self.brief:
            context["Requester-defined product brief (hypothesis and targets, not proof)"] = asdict(self.brief)
        context.update(sections or {})
        return build_prompt(task, question=self.question, clarifications=self.clarifications,
                            evidence=self.ledger.all(), sections=context, **fmt)

    # ------------------------------------------------------------------ phases
    async def run(self, question: str, *, brief: ProductBrief | None = None) -> CouncilOutcome:
        if not isinstance(brief, ProductBrief):
            raise ValueError("A requester-defined ProductBrief is required before the council can start")
        brief.validate()
        self.brief = ProductBrief(brief.hypothesis, brief.expected_result, list(brief.success_metrics))
        self.question = question
        self.emit("start", {"question": question, "voters": self.voters})
        self.emit("brief", asdict(self.brief))
        await self._clarify_phase()
        sweep = " ".join([question, self.brief.hypothesis, self.brief.expected_result,
                          *self.brief.success_metrics, *self.clarifications.values()])
        initial = await self.research.run([ResearchRequest(sweep, [], "initial sweep", "moderator")], 0,
                                          top_k=self.cfg.initial_top_k)
        self.emit("research", {"iteration": 0, "new_evidence": [e.id for e in initial]})

        iterations: list[IterationRecord] = []
        proposal: Proposal | None = None
        status, stop_reason = "no_consensus", "max_iterations"
        for it in range(1, self.cfg.max_iterations + 1):
            rec = IterationRecord(it)
            iterations.append(rec)
            prev = iterations[-2] if len(iterations) > 1 else None
            pending = await self._debate(rec, prev)
            pending += await self._challenge(rec)
            pending += await self._rebut(rec)
            proposal = await self._propose(rec, prev)
            rec.proposal = proposal
            pending = await self._vote(rec) + pending
            rec.consensus = tally(rec.votes, proposal.id, self.cfg.consensus_threshold)
            self.emit("consensus", {"iteration": it, "agree": rec.consensus.agree, "total": rec.consensus.total,
                                    "ratio": rec.consensus.ratio, "reached": rec.consensus.reached})
            if rec.consensus.reached:
                status, stop_reason = "consensus", f"{rec.consensus.ratio:.0%} agreement reached"
                break
            if it == self.cfg.max_iterations:
                break
            rec.research_requests = self._prioritise(pending, rec)
            new = await self.research.run(rec.research_requests, it)
            rec.new_evidence_ids = [e.id for e in new]
            self.emit("research", {"iteration": it, "requests": [r.query for r in rec.research_requests],
                                   "new_evidence": rec.new_evidence_ids})
            if not new and self.cfg.stop_when_research_exhausted:
                stop_reason = "research exhausted: no new evidence found for the council's open questions"
                break

        outcome = CouncilOutcome(
            question=question, status=status, stop_reason=stop_reason, clarifications=self.clarifications,
            iterations=iterations, evidence=self.ledger.all(), claims=list(self.claims.values()),
            final_proposal=proposal, final_consensus=iterations[-1].consensus if iterations else None,
            brief=self.brief,
        )
        self.emit("done", {"status": status, "stop_reason": stop_reason})
        return outcome

    async def _clarify_phase(self) -> None:
        seats = [*DEBATING_ROLES, "skeptic"]
        prompt = self._prompt("clarify")
        results = await asyncio.gather(*(self._call(s, "clarify", prompt, {}) for s in seats), return_exceptions=True)
        candidates = [{"role": s, "question": str(q)} for s, r in zip(seats, results)
                      if isinstance(r, dict) for q in r.get("questions", [])[:2]]
        if not candidates:
            return
        n = self.cfg.max_clarifying_questions
        merged = await self._call("moderator", "consolidate_questions",
                                  self._prompt("consolidate_questions", {"Candidate questions": candidates},
                                               max_questions=n),
                                  {"candidates": candidates, "max_questions": n})
        questions = [str(q) for q in merged.get("questions", [])][:n]
        self.emit("clarify", {"questions": questions})
        if not questions or not self.clarify_fn:
            return
        answers = await self.clarify_fn(questions)
        for i, q in enumerate(questions, 1):
            a = (answers.get(q) or "").strip()
            if a:
                self.clarifications[q] = a
                self.ledger.add(source_type="user_clarification", title=f"Clarification Q{i}",
                                location=f"clarification://Q{i}", excerpt=f"Q: {q}\nA: {a}", iteration=0)

    async def _debate(self, rec: IterationRecord, prev: IterationRecord | None) -> list[ResearchRequest]:
        sections: dict[str, Any] = {}
        if prev:
            sections["Previous round outcome"] = self._round_digest(prev)
        prompt = self._prompt("statement", sections)
        results = await asyncio.gather(*(self._call(r, "statement", prompt, {"iteration": rec.iteration})
                                         for r in DEBATING_ROLES), return_exceptions=True)
        requests: list[ResearchRequest] = []
        for role, res in zip(DEBATING_ROLES, results):
            if isinstance(res, Exception):
                self.emit("error", {"role": role, "task": "statement", "error": str(res)})
                continue
            claim_ids = [self._register_claim(role, rec.iteration, c) for c in res.get("claims", [])
                         if isinstance(c, dict) and c.get("text")]
            rec.statements.append(Statement(role, rec.iteration, str(res.get("position", "conditional")),
                                            str(res.get("summary", "")), claim_ids))
            requests += self._requests(res, role)
            self.emit("statement", {"iteration": rec.iteration, "role": role, "position": res.get("position"),
                                    "summary": res.get("summary")})
        return requests

    def _register_claim(self, role: str, iteration: int, raw: dict[str, Any]) -> str:
        cid = f"C{len(self.claims) + 1:03d}"
        valid, invalid = self.ledger.split_ids([str(i) for i in raw.get("evidence_ids", [])])
        claim = Claim(cid, role, iteration, str(raw["text"]), valid, bool(raw.get("is_assumption", False)),
                      invalid_evidence_ids=invalid)
        # Deterministic evidence gate: a claim with no valid citation can never be "supported".
        if not valid:
            claim.status = "unsupported"
        self.claims[cid] = claim
        return cid

    async def _challenge(self, rec: IterationRecord) -> list[ResearchRequest]:
        claims = [self.claims[c] for s in rec.statements for c in s.claim_ids]
        if not claims:
            return []
        payload = [asdict(c) for c in claims]
        try:
            res = await self._call("skeptic", "skeptic_review",
                                   self._prompt("skeptic_review", {"Claims to audit": payload}),
                                   {"claims": payload, "iteration": rec.iteration})
        except AgentError as exc:
            self.emit("error", {"role": "skeptic", "task": "skeptic_review", "error": str(exc)})
            res = {"verdicts": []}
        reviewed: dict[str, dict] = {str(v.get("claim_id")): v for v in res.get("verdicts", []) if isinstance(v, dict)}
        for claim in claims:
            v = reviewed.get(claim.id, {})
            verdict = str(v.get("verdict", "unsupported" if claim.status == "unsupported" else "weak"))
            if claim.status == "unsupported" or verdict not in ("supported", "weak", "unsupported"):
                verdict = "unsupported" if claim.status == "unsupported" else "weak"
            if claim.is_assumption and verdict == "supported":
                verdict = "weak"
            claim.status = verdict
            if verdict != "supported":
                rec.challenges.append(Challenge(claim.id, claim.role, verdict,
                                                str(v.get("reason", "Not reviewed / no valid evidence")),
                                                str(v.get("required_evidence", ""))))
        rec.skeptic_summary = str(res.get("summary", ""))
        self.emit("challenge", {"iteration": rec.iteration, "challenged": len(rec.challenges),
                                "total": len(claims), "summary": rec.skeptic_summary})
        return self._requests(res, "skeptic")

    async def _rebut(self, rec: IterationRecord) -> list[ResearchRequest]:
        by_role: dict[str, list[Challenge]] = {}
        for ch in rec.challenges:
            by_role.setdefault(ch.target_role, []).append(ch)
        roles = list(by_role)
        calls = []
        for role in roles:
            payload = [{**asdict(ch), "claim_text": self.claims[ch.claim_id].text} for ch in by_role[role]]
            calls.append(self._call(role, "rebuttal", self._prompt("rebuttal", {"Challenges": payload}),
                                    {"challenges": payload, "iteration": rec.iteration}))
        results = await asyncio.gather(*calls, return_exceptions=True)
        requests: list[ResearchRequest] = []
        for role, res in zip(roles, results):
            responses = {} if isinstance(res, Exception) else {
                str(r.get("claim_id")): r for r in res.get("responses", []) if isinstance(r, dict)}
            for ch in by_role[role]:
                claim, r = self.claims[ch.claim_id], responses.get(ch.claim_id)
                action = str(r.get("action")) if r else ""
                if action == "withdraw":
                    claim.status, ch.resolution = "withdrawn", "withdrawn"
                elif action in ("defend", "revise"):
                    valid, invalid = self.ledger.split_ids([str(i) for i in r.get("evidence_ids", [])])
                    if action == "revise" and r.get("text"):
                        claim.text = str(r["text"])
                    if valid:
                        claim.evidence_ids = sorted(set(claim.evidence_ids) | set(valid))
                        claim.status, ch.resolution = "defended", "defended" if action == "defend" else "revised"
                    else:
                        ch.resolution = "unresolved"
                    claim.invalid_evidence_ids = sorted(set(claim.invalid_evidence_ids) | set(invalid))
                else:
                    ch.resolution = "unresolved"
            if not isinstance(res, Exception):
                requests += self._requests(res, role)
        return requests

    async def _propose(self, rec: IterationRecord, prev: IterationRecord | None) -> Proposal:
        sections: dict[str, Any] = {
            "Positions this round": [asdict(s) for s in rec.statements],
            "Claims with audit status": [asdict(self.claims[c]) for s in rec.statements for c in s.claim_ids],
        }
        md: dict[str, Any] = {"iteration": rec.iteration}
        if prev and prev.proposal:
            sections["Previous proposal and votes"] = self._round_digest(prev)
            md["previous_proposal"] = asdict(prev.proposal)
        res = await self._call("moderator", "proposal", self._prompt("proposal", sections), md)
        decision = str(res.get("decision", "defer")).lower()
        valid, _ = self.ledger.split_ids([str(i) for i in res.get("evidence_ids", [])])
        proposal = Proposal(
            id=f"P{rec.iteration}", iteration=rec.iteration, title=str(res.get("title", "Untitled proposal")),
            decision=decision if decision in DECISIONS else "defer", summary=str(res.get("summary", "")),
            scope=_strs(res.get("scope")), out_of_scope=_strs(res.get("out_of_scope")),
            success_metrics=list(dict.fromkeys([
                *(self.brief.success_metrics if self.brief else []), *_strs(res.get("success_metrics")),
            ])),
            risks_and_mitigations=_strs(res.get("risks_and_mitigations")),
            open_questions=_strs(res.get("open_questions")), evidence_ids=valid,
            changes_from_previous=str(res.get("changes_from_previous", "")),
        )
        self.emit("proposal", {"iteration": rec.iteration, "id": proposal.id, "title": proposal.title,
                               "decision": proposal.decision})
        return proposal

    async def _vote(self, rec: IterationRecord) -> list[ResearchRequest]:
        assert rec.proposal is not None
        claims = [asdict(self.claims[c]) for s in rec.statements for c in s.claim_ids]
        sections = {"Proposal under vote": asdict(rec.proposal), "Claims with audit status": claims,
                    "Open challenges": [asdict(c) for c in rec.challenges if c.resolution != "withdrawn"]}
        prompt = self._prompt("vote", sections)
        md = {"iteration": rec.iteration, "proposal": asdict(rec.proposal), "iteration_claims": claims}
        results = await asyncio.gather(*(self._call(r, "vote", prompt, md) for r in self.voters),
                                       return_exceptions=True)
        requests: list[ResearchRequest] = []
        for role, res in zip(self.voters, results):
            if isinstance(res, Exception):
                vote = Vote(role, rec.proposal.id, "disagree", "", [], valid=False, invalid_reason=f"agent error: {res}")
            else:
                choice = str(res.get("vote", "")).lower()
                valid_ids, _ = self.ledger.split_ids([str(i) for i in res.get("evidence_ids", [])])
                vote = Vote(role, rec.proposal.id, choice if choice in ("agree", "disagree") else "disagree",
                            str(res.get("rationale", "")), valid_ids, _strs(res.get("conditions")),
                            str(res.get("what_would_change_my_mind", "")))
                if choice not in ("agree", "disagree"):
                    vote.valid, vote.invalid_reason = False, f"invalid vote value {choice!r}"
                elif choice == "agree" and not valid_ids:
                    vote.valid, vote.invalid_reason = False, "agree vote cited no valid evidence"
                reqs = self._requests(res, role)
                if vote.vote == "disagree" and not reqs and vote.what_would_change_my_mind:
                    reqs = [ResearchRequest(vote.what_would_change_my_mind, [], "dissent condition", role)]
                if not vote.counts_as_agree:
                    requests += reqs
            rec.votes.append(vote)
            self.emit("vote", {"iteration": rec.iteration, "role": role, "vote": vote.vote, "valid": vote.valid,
                               "rationale": vote.rationale})
        return requests

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _requests(res: dict[str, Any], role: str) -> list[ResearchRequest]:
        out = []
        for r in res.get("research_requests", []) or []:
            if isinstance(r, dict) and r.get("query"):
                types = [t for t in _strs(r.get("source_types")) if t in SOURCE_TYPES]
                out.append(ResearchRequest(str(r["query"]), types, str(r.get("rationale", "")), role))
        return out

    def _prioritise(self, pending: list[ResearchRequest], rec: IterationRecord) -> list[ResearchRequest]:
        """Dedupe; dissenting voters' requests come first (they were prepended), capped per round."""
        seen, out = set(), []
        for r in pending:
            key = (" ".join(r.query.lower().split()), tuple(sorted(r.source_types)))
            if key not in seen:
                seen.add(key)
                out.append(r)
        if not out:
            out = [ResearchRequest(f"{self.question} {v.rationale}", [], "fallback for dissent", v.role)
                   for v in rec.votes if not v.counts_as_agree]
        return out[: self.cfg.max_research_requests]

    def _round_digest(self, rec: IterationRecord) -> dict[str, Any]:
        return {
            "proposal": asdict(rec.proposal) if rec.proposal else None,
            "consensus": f"{rec.consensus.agree}/{rec.consensus.total}" if rec.consensus else None,
            "votes": [{"role": v.role, "vote": v.vote, "valid": v.valid, "rationale": v.rationale,
                       "conditions": v.conditions, "what_would_change_my_mind": v.what_would_change_my_mind}
                      for v in rec.votes],
            "unresolved_challenges": [asdict(c) for c in rec.challenges if c.resolution in ("open", "unresolved")],
            "new_evidence_since": rec.new_evidence_ids,
        }


def _strs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []
