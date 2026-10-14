import asyncio
import json
import unittest
from pathlib import Path

from product_council import Council, CouncilConfig, load_config, render_markdown, tally
from product_council.config import force_mock
from product_council.orchestrator import build_sources
from product_council.prompts import parse_json
from product_council.providers import MockProvider
from product_council.research import EvidenceLedger, LocalCorpusSource
from product_council.schemas import Vote

ROOT = Path(__file__).resolve().parents[1]
QUESTION = (ROOT / "examples" / "day2_question.txt").read_text(encoding="utf-8").strip()


def _config() -> CouncilConfig:
    return force_mock(load_config(ROOT / "council.toml"))


def _run(council: Council, question: str = QUESTION):
    return asyncio.run(council.run(question))


class HallucinatingSkeptic(MockProvider):
    """Skeptic that rubber-stamps everything, and seats that cite nonexistent evidence."""

    def _skeptic_review(self, md):
        return {"verdicts": [{"claim_id": c["id"], "verdict": "supported", "reason": "ok"} for c in md["claims"]],
                "research_requests": [], "summary": "all good"}

    def _statement(self, md):
        return {"position": "support", "summary": "s",
                "claims": [{"text": "Made-up stat", "evidence_ids": ["E999"]}], "research_requests": []}

    def _vote(self, md):
        return {"vote": "agree", "rationale": "trust me", "evidence_ids": ["E999"]}


class FailingProvider(MockProvider):
    async def complete(self, request):
        if request.metadata.get("task") == "vote" and request.metadata.get("role") == "security":
            raise RuntimeError("model unavailable")
        return await super().complete(request)


class ConsensusMathTests(unittest.TestCase):
    def _votes(self, agree: int, total: int):
        return [Vote(f"r{i}", "P1", "agree" if i < agree else "disagree", "", ["E001"]) for i in range(total)]

    def test_threshold_with_seven_voters(self):
        self.assertFalse(tally(self._votes(5, 7), "P1", 0.75).reached)  # 71%
        self.assertTrue(tally(self._votes(6, 7), "P1", 0.75).reached)   # 86%

    def test_exact_threshold_passes(self):
        self.assertTrue(tally(self._votes(3, 4), "P1", 0.75).reached)

    def test_invalid_agree_does_not_count(self):
        votes = self._votes(7, 7)
        votes[0].valid = False
        result = tally(votes, "P1", 0.75)
        self.assertEqual(result.agree, 6)


class EndToEndTests(unittest.TestCase):
    def test_research_loop_reaches_consensus(self):
        answers = json.loads((ROOT / "examples" / "answers.json").read_text(encoding="utf-8"))

        async def clarify(questions):
            return {q: answers.get(str(i), "") for i, q in enumerate(questions, 1)}

        cfg = _config()
        outcome = _run(Council(cfg, clarify=clarify, retry_backoff=0))
        self.assertEqual(outcome.status, "consensus")
        self.assertGreaterEqual(len(outcome.iterations), 2)
        first = outcome.iterations[0]
        self.assertLess(first.consensus.ratio, 0.75)
        self.assertTrue(first.research_requests)
        self.assertTrue(first.new_evidence_ids)
        self.assertGreaterEqual(outcome.final_consensus.ratio, 0.75)
        self.assertEqual(outcome.final_consensus.total, 7)
        self.assertTrue(any(e.source_type == "user_clarification" for e in outcome.evidence))
        self.assertTrue(outcome.clarifications)
        memo = render_markdown(outcome)
        self.assertIn("CONSENSUS", memo)
        json.dumps(outcome.to_dict(), default=str)

    def test_no_evidence_means_no_consensus(self):
        cfg = _config()
        outcome = _run(Council(cfg, sources=[], retry_backoff=0))
        self.assertEqual(outcome.status, "no_consensus")
        self.assertIn("research exhausted", outcome.stop_reason)
        self.assertIn("NO CONSENSUS", render_markdown(outcome))

    def test_max_iterations_respected(self):
        cfg = _config()
        cfg.max_iterations = 1
        outcome = _run(Council(cfg, retry_backoff=0))
        self.assertEqual(len(outcome.iterations), 1)
        self.assertEqual(outcome.status, "no_consensus")

    def test_hallucinated_evidence_is_rejected_even_if_skeptic_approves(self):
        cfg = _config()
        outcome = _run(Council(cfg, providers={"mock": HallucinatingSkeptic()}, retry_backoff=0))
        rec = outcome.iterations[0]
        for claim in outcome.claims:
            self.assertEqual(claim.invalid_evidence_ids, ["E999"])
            self.assertNotIn(claim.status, ("supported", "defended"))
        self.assertTrue(all(not v.counts_as_agree for v in rec.votes))
        self.assertEqual(outcome.status, "no_consensus")

    def test_agent_failure_counts_as_non_agree(self):
        cfg = _config()
        cfg.max_iterations = 1
        outcome = _run(Council(cfg, providers={"mock": FailingProvider()}, retry_backoff=0))
        vote = next(v for v in outcome.iterations[0].votes if v.role == "security")
        self.assertFalse(vote.valid)
        self.assertIn("agent error", vote.invalid_reason)

    def test_skeptic_can_be_non_voting(self):
        cfg = _config()
        cfg.skeptic_votes = False
        outcome = _run(Council(cfg, retry_backoff=0))
        self.assertEqual(outcome.final_consensus.total, 6)


class ComponentTests(unittest.TestCase):
    def test_parse_json_handles_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Sure! {"a": 2}'), {"a": 2})
        with self.assertRaises(ValueError):
            parse_json("no json here")

    def test_ledger_dedupes_and_validates(self):
        ledger = EvidenceLedger()
        e = ledger.add(source_type="internal_doc", title="t", location="x", excerpt="y", iteration=0)
        self.assertIsNone(ledger.add(source_type="internal_doc", title="t", location="x", excerpt="y", iteration=1))
        self.assertEqual(ledger.split_ids([e.id, "E404"]), ([e.id], ["E404"]))

    def test_local_corpus_search(self):
        src = LocalCorpusSource(ROOT / "knowledge" / "internal_docs", "internal_doc")
        hits = asyncio.run(src.search("support case volume after migration", 1))
        self.assertEqual(len(hits), 1)
        self.assertIn("case volume", hits[0].excerpt.lower())

    def test_config_loads_and_resolves_sources(self):
        cfg = load_config(ROOT / "council.toml")
        self.assertEqual(cfg.consensus_threshold, 0.75)
        self.assertEqual(len(build_sources(cfg)), 5)
        self.assertEqual(set(cfg.seats), {"customer_success", "engineering", "product", "marketing", "security",
                                          "support", "skeptic"})


if __name__ == "__main__":
    unittest.main()
