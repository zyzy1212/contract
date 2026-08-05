from types import SimpleNamespace

from app.review.generator import (
    DraftClauseReview,
    GeneratedFinding,
    _evidence_aliases,
    _evidence_block,
    _resolve_draft_ids,
)


def _candidate(chunk_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(chunk_id=chunk_id, text=text)


def test_evidence_block_uses_short_aliases() -> None:
    candidates = [
        _candidate("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "证据一"),
        _candidate("22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "证据二"),
    ]
    block = _evidence_block(candidates)
    assert "[E1]" in block
    assert "[E2]" in block
    assert "11111111-aaaa" not in block


def test_resolve_draft_ids_maps_aliases() -> None:
    draft = DraftClauseReview(
        findings=[
            GeneratedFinding(
                title="标题",
                risk_level="high",
                problem="问题",
                reason="理由",
                suggestion="建议",
                proposed_clause="参考条款",
                evidence_ids=["E1"],
            )
        ]
    )
    aliases = _evidence_aliases(
        [_candidate("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "证据一")]
    )
    resolved = _resolve_draft_ids(draft, aliases)
    assert resolved.findings[0].evidence_ids == [
        "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ]
