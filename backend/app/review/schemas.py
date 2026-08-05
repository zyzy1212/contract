from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.retrieval.models import EvidenceCandidate


class GeneratedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str
    risk_level: Literal["high", "medium", "low"]
    problem: str
    reason: str
    suggestion: str
    proposed_clause: str
    evidence_ids: list[str] = Field(min_length=1)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["pass", "revise", "reject"]
    unsupported_fields: list[str] = Field(default_factory=list)
    evidence_gap: bool = False
    feedback: str = ""


class EvidenceCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["sufficient", "insufficient"]
    rounds: int
    evidence_ids: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    candidates: list[EvidenceCandidate] = Field(default_factory=list)


class FinalClauseReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["complete", "needs_retrieval", "review_failed"]
    findings: list[GeneratedFinding] = Field(default_factory=list)
    review_decision: ReviewDecision
