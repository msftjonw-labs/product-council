"""Evidence ledger and research sources.

Sources are pluggable. The reference implementation ships a local-corpus source (markdown/text folders
for internal docs, interview notes, vision docs, partner briefs) and a human-in-the-loop source. Enterprise
deployments add sources such as Azure AI Search, Microsoft Graph/SharePoint, or interview repositories by
implementing ``ResearchSource``.
"""
from __future__ import annotations

import abc
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .schemas import Evidence, ResearchRequest

_STOP = set(
    "the a an and or of to in on for with is are be by as at it this that from was were has have had not "
    "but if than then so into over after before their there them they we our you your its can will should "
    "would could about what which who how when where why do does did more most less such any all each".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower()) if t not in _STOP]


@dataclass
class SearchHit:
    source_type: str
    title: str
    location: str
    excerpt: str
    score: float


class ResearchSource(abc.ABC):
    source_type: str

    @abc.abstractmethod
    async def search(self, query: str, top_k: int) -> list[SearchHit]:
        ...


class LocalCorpusSource(ResearchSource):
    """TF-IDF search over paragraph chunks of .md/.txt files in a folder."""

    def __init__(self, root: str | Path, source_type: str) -> None:
        self.root = Path(root)
        self.source_type = source_type
        self._chunks: list[tuple[str, str, str, Counter]] = []  # title, location, text, tf
        self._df: Counter = Counter()
        self._load()

    def _load(self) -> None:
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*")):
            if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            heading = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), path.stem)
            for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", text)):
                if not para or (para.startswith("#") and "\n" not in para):
                    continue
                tf = Counter(tokenize(para))
                if not tf:
                    continue
                self._chunks.append((heading, f"{path.as_posix()}#p{i}", para, tf))
                self._df.update(tf.keys())

    async def search(self, query: str, top_k: int) -> list[SearchHit]:
        terms = set(tokenize(query))
        n = len(self._chunks) or 1
        hits = []
        for title, loc, text, tf in self._chunks:
            score = sum(tf[t] * math.log(1 + n / self._df[t]) for t in terms if t in tf)
            if score > 0:
                hits.append(SearchHit(self.source_type, title, loc, text, score / math.sqrt(sum(tf.values()))))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]


AskFn = Callable[[str], Awaitable[str]]


class HumanSource(ResearchSource):
    """Asks a human (e.g. the PM running the council) to supply data such as fresh customer interview notes."""

    source_type = "user_provided"

    def __init__(self, ask: AskFn) -> None:
        self._ask = ask

    async def search(self, query: str, top_k: int) -> list[SearchHit]:
        answer = (await self._ask(query)).strip()
        if not answer:
            return []
        return [SearchHit(self.source_type, "Human-provided data", "human://council-operator", answer, 1.0)]


class EvidenceLedger:
    """Append-only, de-duplicated store of evidence. IDs are the only thing agents may cite."""

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self._fingerprints: set[str] = set()

    def __contains__(self, evidence_id: str) -> bool:
        return evidence_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def add(self, *, source_type: str, title: str, location: str, excerpt: str, iteration: int,
            query: str = "") -> Evidence | None:
        fp = hashlib.sha256(f"{location}|{excerpt}".encode()).hexdigest()
        if fp in self._fingerprints:
            return None
        self._fingerprints.add(fp)
        ev = Evidence(f"E{len(self._items) + 1:03d}", source_type, title, location, excerpt, iteration, query)
        self._items[ev.id] = ev
        return ev

    def split_ids(self, ids: list[str]) -> tuple[list[str], list[str]]:
        """Return (valid, invalid) evidence IDs."""
        valid = [i for i in ids if i in self._items]
        return valid, [i for i in ids if i not in self._items]


class ResearchEngine:
    def __init__(self, sources: list[ResearchSource], ledger: EvidenceLedger, top_k: int = 3) -> None:
        self.sources = sources
        self.ledger = ledger
        self.top_k = top_k

    async def run(self, requests: list[ResearchRequest], iteration: int, top_k: int | None = None) -> list[Evidence]:
        k = top_k or self.top_k
        new: list[Evidence] = []
        automated = [s for s in self.sources if not isinstance(s, HumanSource)]
        humans = [s for s in self.sources if isinstance(s, HumanSource)]
        for req in requests:
            wanted = set(req.source_types)
            for src in automated:
                if wanted and src.source_type not in wanted:
                    continue
                new.extend(await self._ingest(src, req.query, iteration, k))
        # Humans are asked once per research round (never during the initial sweep) with the open requests.
        if humans and requests and iteration > 0:
            agenda = "\n".join(f"- [{r.requested_by}] {r.query} (wants: {', '.join(r.source_types) or 'any'})"
                               for r in requests)
            for src in humans:
                new.extend(await self._ingest(src, agenda, iteration, k))
        return new

    async def _ingest(self, src: ResearchSource, query: str, iteration: int, top_k: int) -> list[Evidence]:
        out = []
        for hit in await src.search(query, top_k):
            ev = self.ledger.add(source_type=hit.source_type, title=hit.title, location=hit.location,
                                 excerpt=hit.excerpt, iteration=iteration, query=query)
            if ev:
                out.append(ev)
        return out
