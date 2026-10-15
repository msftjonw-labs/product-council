import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from product_council import Council, CouncilConfig, ProductBrief, load_config, render_markdown, tally
from product_council.cli import _interactive_brief, main
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
    return asyncio.run(council.run(question, brief=_brief()))


def _brief() -> ProductBrief:
    return ProductBrief.from_dict(json.loads((ROOT / "examples" / "brief.json").read_text(encoding="utf-8")))


class RecordingProvider(MockProvider):
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await super().complete(request)


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

    def test_threshold_with_eight_voters(self):
        self.assertFalse(tally(self._votes(5, 8), "P1", 0.75).reached)
        self.assertTrue(tally(self._votes(6, 8), "P1", 0.75).reached)

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
        self.assertEqual(outcome.final_consensus.total, 8)
        self.assertTrue(any(e.source_type == "user_clarification" for e in outcome.evidence))
        self.assertTrue(outcome.clarifications)
        memo = render_markdown(outcome)
        self.assertIn("CONSENSUS", memo)
        self.assertIn("Forecast Agent", memo)
        self.assertIn(outcome.brief.hypothesis, memo)
        self.assertIn(outcome.brief.expected_result, memo)
        for metric in outcome.brief.success_metrics:
            self.assertIn(metric, memo)
        self.assertEqual(outcome.to_dict()["brief"], json.loads(
            (ROOT / "examples" / "brief.json").read_text(encoding="utf-8")))
        for rec in outcome.iterations:
            self.assertTrue(any(s.role == "forecast" and "days 61-90" in s.summary for s in rec.statements))
            self.assertTrue(any(v.role == "forecast" for v in rec.votes))
            for metric in outcome.brief.success_metrics:
                self.assertIn(metric, rec.proposal.success_metrics)
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
        self.assertEqual(outcome.final_consensus.total, 7)

    def test_brief_required_before_any_agent_calls_or_events(self):
        provider = RecordingProvider()
        events = []
        council = Council(_config(), providers={"mock": provider}, on_event=lambda *e: events.append(e))
        with self.assertRaisesRegex(ValueError, "requester-defined ProductBrief"):
            asyncio.run(council.run(QUESTION))
        with self.assertRaisesRegex(ValueError, "hypothesis"):
            asyncio.run(council.run(QUESTION, brief=ProductBrief(" ", "result", ["metric"])))
        self.assertEqual(provider.requests, [])
        self.assertEqual(events, [])
        self.assertEqual(council.ledger.all(), [])

    def test_every_agent_task_receives_brief_without_treating_it_as_evidence(self):
        provider = RecordingProvider()
        _run(Council(_config(), providers={"mock": provider}, retry_backoff=0))
        for request in provider.requests:
            self.assertEqual(request.metadata["brief"]["hypothesis"], _brief().hypothesis)
            self.assertIn(_brief().hypothesis, request.user)
            self.assertIn(_brief().expected_result, request.user)
            self.assertIn("hypothesis and targets, not proof", request.user)
            self.assertTrue(all(_brief().hypothesis not in e["excerpt"]
                                for e in request.metadata["evidence"]))
        forecast_tasks = {r.metadata["task"] for r in provider.requests if r.metadata["role"] == "forecast"}
        self.assertTrue({"clarify", "statement", "rebuttal", "vote"} <= forecast_tasks)


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
                                          "support", "forecast", "skeptic"})

    def test_product_brief_rejects_missing_blank_and_wrongly_typed_fields(self):
        valid = {"hypothesis": "hypothesis", "expected_result": "result", "success_metrics": ["metric"]}
        invalid = [None, [], {}, {**valid, "extra": "value"}]
        for key in valid:
            invalid.append({k: v for k, v in valid.items() if k != key})
        for key in ("hypothesis", "expected_result"):
            for value in ("", " \n", None, 123, []):
                invalid.append({**valid, key: value})
        for value in ([], "", "metric", None, [" "], ["metric", None], [123]):
            invalid.append({**valid, "success_metrics": value})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                ProductBrief.from_dict(data)
        self.assertEqual(ProductBrief.from_dict(valid).success_metrics, ["metric"])


class CliTests(unittest.TestCase):
    def test_noninteractive_run_requires_brief(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exc:
            main(["run", "-q", QUESTION, "--mock", "--no-clarify"])
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("--brief is required", err.getvalue())

    def test_invalid_brief_is_reported_before_council_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            for text in ("not JSON", "{}", '{"hypothesis": "h", "expected_result": "r", "success_metrics": []}'):
                path.write_text(text, encoding="utf-8")
                with (self.subTest(text=text), patch("product_council.cli.Council") as council,
                      contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit)):
                    main(["run", "-q", QUESTION, "--brief", str(path), "--mock", "--no-clarify"])
                self.assertIn("Invalid --brief", err.getvalue())
                council.assert_not_called()

    def test_interactive_brief_reprompts_for_required_values(self):
        with patch("builtins.input", side_effect=[" ", "Hypothesis", "", "Result", "", "20% by day 90", ""]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            brief = _interactive_brief()
        brief.validate()
        self.assertEqual(brief, ProductBrief("Hypothesis", "Result", ["20% by day 90"]))
        self.assertEqual(output.getvalue().count("This field is required."), 3)

    def test_cli_persists_brief_and_forecast(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            code = main(["run", "--question-file", str(ROOT / "examples" / "day2_question.txt"),
                         "--config", str(ROOT / "council.toml"), "--brief", str(ROOT / "examples" / "brief.json"),
                         "--answers", str(ROOT / "examples" / "answers.json"), "--mock", "--out", tmp])
            self.assertEqual(code, 0)
            transcript = json.loads((Path(tmp) / "transcript.json").read_text(encoding="utf-8"))
            self.assertEqual(transcript["brief"]["hypothesis"], _brief().hypothesis)
            self.assertEqual(transcript["final_consensus"]["total"], 8)
            self.assertIn("Forecast Agent", (Path(tmp) / "decision.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
