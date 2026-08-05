from types import SimpleNamespace

from app.review.evidence_judge import (
    EvidenceDecision,
    _evidence_aliases,
    _judge_request,
    _resolve_decision_ids,
)


def _candidate(chunk_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(chunk_id=chunk_id, text=text)


def test_judge_request_uses_short_aliases() -> None:
    candidates = [
        _candidate("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "证据一"),
        _candidate("22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "证据二"),
    ]
    request = _judge_request("待审核条款", candidates)
    assert "[E1]" in request
    assert "[E2]" in request
    assert "11111111-aaaa" not in request
    assert "22222222-bbbb" not in request


def test_resolve_decision_ids_maps_aliases() -> None:
    candidates = [
        _candidate("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "证据一"),
        _candidate("22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "证据二"),
    ]
    aliases = _evidence_aliases(candidates)
    decision = EvidenceDecision(
        sufficient=True,
        usable_evidence_ids=["E1", "E2"],
        rejected_evidence={"E1": "与本条款不直接相关"},
    )
    resolved = _resolve_decision_ids(decision, aliases)
    assert resolved.usable_evidence_ids == [
        "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]
    assert "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa" in resolved.rejected_evidence


def test_resolve_decision_ids_keeps_unknown_ids() -> None:
    decision = EvidenceDecision(sufficient=True, usable_evidence_ids=["E9"])
    resolved = _resolve_decision_ids(decision, {"E1": "known-id"})
    assert resolved.usable_evidence_ids == ["E9"]
