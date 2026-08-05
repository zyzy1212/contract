import pytest

from app.review.orchestrator import (
    _grounding_value_matches,
    _normalize_cn_numbers,
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
