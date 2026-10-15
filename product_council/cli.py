"""Command-line entry point: python -m product_council run --question "..." [--config council.toml]."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config import CouncilConfig, force_mock, load_config
from .orchestrator import Council, build_sources
from .report import render_markdown
from .schemas import ProductBrief


def _print_event(kind: str, p: dict[str, Any]) -> None:
    if kind == "start":
        print(f"\n== Product Council convened ({len(p['voters'])} voting seats) ==")
    elif kind == "brief":
        print(f"\n-- Requester-defined product brief --\nHypothesis: {p['hypothesis']}\n"
              f"Expected result: {p['expected_result']}\nSuccess metrics:")
        for metric in p["success_metrics"]:
            print(f"  - {metric}")
    elif kind == "clarify":
        print(f"\n-- Clarifying questions ({len(p['questions'])}) --")
        for i, q in enumerate(p["questions"], 1):
            print(f"  Q{i}. {q}")
    elif kind == "research":
        print(f"[research] iteration {p['iteration']}: +{len(p['new_evidence'])} evidence")
    elif kind == "statement":
        print(f"[it{p['iteration']}] {p['role']:<16} {p['position']}: {p['summary']}")
    elif kind == "challenge":
        print(f"[it{p['iteration']}] skeptic          challenged {p['challenged']}/{p['total']} claims")
    elif kind == "proposal":
        print(f"[it{p['iteration']}] moderator        proposal {p['id']} ({p['decision']}): {p['title']}")
    elif kind == "vote":
        flag = "" if p["valid"] else " (invalid)"
        print(f"[it{p['iteration']}] vote {p['role']:<11} {p['vote']}{flag}")
    elif kind == "consensus":
        mark = "REACHED" if p["reached"] else "not reached -> researching more data"
        print(f"[it{p['iteration']}] consensus {p['agree']}/{p['total']} = {p['ratio']:.0%} {mark}")
    elif kind == "error":
        print(f"[error] {p['role']}/{p['task']}: {p['error']}", file=sys.stderr)
    elif kind == "done":
        print(f"\n== {p['status'].upper()}: {p['stop_reason']} ==")


async def _interactive_clarify(questions: list[str]) -> dict[str, str]:
    answers = {}
    for i, q in enumerate(questions, 1):
        answers[q] = await asyncio.to_thread(input, f"Q{i}. {q}\n> ")
    return answers


async def _interactive_research(agenda: str) -> str:
    print("\nThe council needs more data. Open research requests:\n" + agenda)
    print("Paste any data you have (with its source); finish with an empty line. Leave blank to skip.")
    lines = []
    while True:
        line = await asyncio.to_thread(input, "")
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


def _file_clarify(path: Path):
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))

    async def clarify(questions: list[str]) -> dict[str, str]:
        # Answers file may be keyed by exact question text or by position ("1", "2", ...).
        return {q: data.get(q) or data.get(str(i), "") for i, q in enumerate(questions, 1)}

    return clarify


def _interactive_brief() -> ProductBrief:
    print("Define the product brief before the council starts (all fields are required).")

    def required(label: str) -> str:
        while True:
            value = input(label + "\n> ").strip()
            if value:
                return value
            print("This field is required.")

    hypothesis = required("Hypothesis: what change do you believe will address the product-market gap?")
    expected_result = required("Expected result: what outcome should follow release?")
    metrics = [required("Success metric: include a measurable target and evaluation timeframe.")]
    while metric := input("Another success metric (leave blank to finish):\n> ").strip():
        metrics.append(metric)
    return ProductBrief(hypothesis, expected_result, metrics)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="product-council", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Convene the council on a question")
    run.add_argument("--question", "-q", help="Question text (or use --question-file)")
    run.add_argument("--question-file", type=Path)
    run.add_argument("--config", "-c", type=Path, default=Path("council.toml"))
    run.add_argument("--mock", action="store_true", help="Force every seat onto the offline mock provider")
    run.add_argument("--answers", type=Path, help="JSON file with clarifying answers (non-interactive)")
    run.add_argument("--brief", type=Path,
                     help="Requester JSON with hypothesis, expected_result, and success_metrics (list of strings)")
    run.add_argument("--no-clarify", action="store_true", help="Skip asking clarifying questions")
    run.add_argument("--interactive-research", action="store_true",
                     help="Let the operator supply extra data (e.g. new interview notes) between iterations")
    run.add_argument("--out", type=Path, default=Path("out"))
    run.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    question = args.question or (args.question_file.read_text(encoding="utf-8").strip() if args.question_file else "")
    if not question:
        ap.error("--question or --question-file is required")

    if args.brief:
        try:
            brief = ProductBrief.from_dict(json.loads(args.brief.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            ap.error(f"Invalid --brief: {exc}")
    elif args.answers or args.no_clarify or not sys.stdin.isatty():
        ap.error("--brief is required for non-interactive runs and with --no-clarify")
    else:
        brief = _interactive_brief()
    brief.validate()

    cfg = load_config(args.config) if args.config.exists() else CouncilConfig()
    if args.mock:
        cfg = force_mock(cfg)

    clarify = None
    if args.answers:
        clarify = _file_clarify(args.answers)
    elif not args.no_clarify:
        clarify = _interactive_clarify
    sources = build_sources(cfg, _interactive_research if args.interactive_research else None)

    council = Council(cfg, sources=sources, clarify=clarify, on_event=_print_event)
    outcome = asyncio.run(council.run(question, brief=brief))

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "decision.md").write_text(render_markdown(outcome), encoding="utf-8")
    (args.out / "transcript.json").write_text(json.dumps(outcome.to_dict(), indent=2, default=str), encoding="utf-8")
    print(f"Wrote {args.out / 'decision.md'} and {args.out / 'transcript.json'}")
    return 0 if outcome.status == "consensus" else 2
