import asyncio

import pytest

from app.auth import Actor
from app.retrieval.models import EvidenceCandidate
from app.review.evidence_judge import EvidenceDecision
from app.review.orchestrator import (
    _grounding_value_matches,
    _normalize_cn_numbers,
    _validate_judge_evidence,
    collect_sufficient_evidence,
    validate_grounding,
)
from app.review.schemas import GeneratedFinding


def _finding(*, reason: str) -> GeneratedFinding:
    return GeneratedFinding(
        title="标题",
        risk_level="high",
        problem="问题",
        reason=reason,
        suggestion="建议",
        proposed_clause="参考条款",
        evidence_ids=["evidence-1"],
    )


def test_normalize_cn_numbers() -> None:
    assert _normalize_cn_numbers("第一百二十条") == "第120条"
    assert _normalize_cn_numbers("二零一八年") == "2018年"
    assert _normalize_cn_numbers("三千零五") == "3005"


def test_grounding_value_matches_number_boundary() -> None:
    assert _grounding_value_matches("第120条", "120")
    assert not _grounding_value_matches("第120条", "12")
    assert _grounding_value_matches("2021年1月1日", "2021")


def test_validate_grounding_accepts_chinese_numeral_evidence() -> None:
    finding = _finding(reason="依据第一百二十条确定")
    validate_grounding(
        finding,
        {"evidence-1"},
        evidence_by_id={"evidence-1": "民法典第一百二十条规定了相关责任。"},
    )


def test_validate_grounding_rejects_unsupported_number() -> None:
    finding = _finding(reason="赔偿金额为6510元")
    with pytest.raises(ValueError):
        validate_grounding(
            finding,
            {"evidence-1"},
            evidence_by_id={"evidence-1": "民法典规定了违约责任，未涉及金额。"},
        )


def test_validate_grounding_skips_single_digit_values() -> None:
    finding = _finding(reason="见第4项建议")
    validate_grounding(
        finding,
        {"evidence-1"},
        evidence_by_id={"evidence-1": "民法典规定了违约责任。"},
    )


def test_validate_judge_evidence_rejects_unknown_ids() -> None:
    decision = EvidenceDecision(sufficient=True, usable_evidence_ids=["unknown-1"])
    with pytest.raises(ValueError, match="unknown evidence ids"):
        _validate_judge_evidence(decision, {"known-1"})


def _evidence_candidate(chunk_id: str, vector_score: float) -> EvidenceCandidate:
    return EvidenceCandidate(
        chunk_id=chunk_id,
        text=f"候选证据 {chunk_id}",
        source_snapshot_id=f"snapshot-{chunk_id}",
        score=1.0,
        rank=1,
        channel_scores={"vector": vector_score},
    )


class _FakeEvidenceRetriever:
    def __init__(self, candidates) -> None:
        self._candidates = candidates

    async def search(self, actor, query, excluded_chunk_ids=()):
        return self._candidates


class _RecordingJudge:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def evaluate(self, query, candidates):
        self.seen.extend(item.chunk_id for item in candidates)
        return EvidenceDecision(
            sufficient=True,
            usable_evidence_ids=[item.chunk_id for item in candidates],
        )


def test_low_similarity_evidence_is_filtered_before_llm() -> None:
    retriever = _FakeEvidenceRetriever(
        [
            _evidence_candidate("low", 0.2),
            _evidence_candidate("high", 0.9),
        ]
    )
    judge = _RecordingJudge()
    collection = asyncio.run(
        collect_sufficient_evidence(
            Actor(user_id="user-a", tenant_id="tenant-a", role="customer"),
            "合同成立与生效",
            retriever,
            judge,
            min_similarity=0.5,
        )
    )
    assert judge.seen == ["high"]
    assert [item.chunk_id for item in collection.candidates] == ["high"]
