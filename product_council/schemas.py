"""Data contracts shared by every Product Council component."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SOURCE_TYPES = (
    "internal_doc",
    "customer_interview",
    "team_vision",
    "company_vision",
    "partnership",
    "user_clarification",
    "user_provided",
)

DECISIONS = ("build", "pilot", "partner", "defer", "do_not_build")


@dataclass
class ProductBrief:
    hypothesis: str
    expected_result: str
    success_metrics: list[str]

    def validate(self) -> None:
        for name in ("hypothesis", "expected_result"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Product brief {name} must be a non-empty string")
        if (not isinstance(self.success_metrics, list) or not self.success_metrics
                or any(not isinstance(m, str) or not m.strip() for m in self.success_metrics)):
            raise ValueError("Product brief success_metrics must be a non-empty list of non-empty strings")

    @classmethod
    def from_dict(cls, data: Any) -> ProductBrief:
        if not isinstance(data, dict):
            raise ValueError("Product brief must be a JSON object")
        required = {"hypothesis", "expected_result", "success_metrics"}
        if set(data) != required:
            raise ValueError("Product brief must contain exactly: hypothesis, expected_result, success_metrics")
        brief = cls(**data)
        brief.validate()
        return brief


@dataclass
class Evidence:
    id: str
    source_type: str
    title: str
    location: str
    excerpt: str
    retrieved_iteration: int
    query: str = ""

    def brief(self, max_chars: int = 600) -> str:
        text = self.excerpt if len(self.excerpt) <= max_chars else self.excerpt[:max_chars] + "..."
        return f"[{self.id}] ({self.source_type}) {self.title} <{self.location}>\n{text}"


@dataclass
class ResearchRequest:
    query: str
    source_types: list[str] = field(default_factory=list)
    rationale: str = ""
    requested_by: str = ""


@dataclass
class Claim:
    id: str
    role: str
    iteration: int
    text: str
    evidence_ids: list[str]
    is_assumption: bool = False
    # unverified -> supported | weak | unsupported -> defended | withdrawn
    status: str = "unverified"
    invalid_evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Statement:
    role: str
    iteration: int
    position: str  # support | oppose | conditional
    summary: str
    claim_ids: list[str]


@dataclass
class Challenge:
    claim_id: str
    target_role: str
    verdict: str  # weak | unsupported
    reason: str
    required_evidence: str = ""
    resolution: str = "open"  # open | defended | revised | withdrawn | unresolved


@dataclass
class Proposal:
    id: str
    iteration: int
    title: str
    decision: str
    summary: str
    scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    success_metrics: list[str] = field(default_factory=list)
    risks_and_mitigations: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    changes_from_previous: str = ""


@dataclass
class Vote:
    role: str
    proposal_id: str
    vote: str  # agree | disagree
    rationale: str
    evidence_ids: list[str]
    conditions: list[str] = field(default_factory=list)
    what_would_change_my_mind: str = ""
    valid: bool = True
    invalid_reason: str = ""

    @property
    def counts_as_agree(self) -> bool:
        return self.vote == "agree" and self.valid


@dataclass
class ConsensusResult:
    proposal_id: str
    agree: int
    total: int
    threshold: float

    @property
    def ratio(self) -> float:
        return self.agree / self.total if self.total else 0.0

    @property
    def reached(self) -> bool:
        return self.total > 0 and self.ratio >= self.threshold


@dataclass
class IterationRecord:
    iteration: int
    statements: list[Statement] = field(default_factory=list)
    challenges: list[Challenge] = field(default_factory=list)
    proposal: Proposal | None = None
    votes: list[Vote] = field(default_factory=list)
    consensus: ConsensusResult | None = None
    research_requests: list[ResearchRequest] = field(default_factory=list)
    new_evidence_ids: list[str] = field(default_factory=list)
    skeptic_summary: str = ""


@dataclass
class CouncilOutcome:
    question: str
    status: str  # consensus | no_consensus
    stop_reason: str
    clarifications: dict[str, str]
    iterations: list[IterationRecord]
    evidence: list[Evidence]
    claims: list[Claim]
    final_proposal: Proposal | None
    final_consensus: ConsensusResult | None
    brief: ProductBrief

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for rec, raw in zip(self.iterations, data["iterations"]):
            if rec.consensus and raw["consensus"] is not None:
                raw["consensus"]["ratio"] = rec.consensus.ratio
                raw["consensus"]["reached"] = rec.consensus.reached
        if self.final_consensus and data["final_consensus"] is not None:
            data["final_consensus"]["ratio"] = self.final_consensus.ratio
            data["final_consensus"]["reached"] = self.final_consensus.reached
        return data
