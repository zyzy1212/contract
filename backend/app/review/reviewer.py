from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.llm.deepseek import DeepSeekClient
from app.review.generator import DraftClauseReview
from app.review.schemas import ReviewDecision
from app.retrieval.models import EvidenceCandidate


class ResultReviewer(Protocol):
    async def review(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
        draft: DraftClauseReview,
    ) -> ReviewDecision: ...


_REVIEWER_SYSTEM_PROMPT = (
    "你是独立的合同审核结果复审 Agent。逐字段检查生成结果是否被指定证据直接支持。\n"
    "检查每个 evidence_ids 是否都在提供的证据中，且证据文本确实支持结论。\n"
    "检查金额、比例、期限、日期等确定性数值是否都出现在证据原文中。\n"
    "检查修改建议和参考条款是否引入证据之外的新事实或义务。\n"
    "decision 只能是 pass、revise、reject 三选一；evidence_gap 必须是 JSON 布尔值；"
    "unsupported_fields 必须是字符串数组；feedback 必须是字符串。\n"
    "必须使用 JSON 输出。"
)


def _evidence_block(evidence: Sequence[EvidenceCandidate]) -> str:
    return "\n".join(f"[{item.chunk_id}] {item.text}" for item in evidence)


def _review_request(
    clause: Mapping[str, Any] | str,
    evidence: Sequence[EvidenceCandidate],
    draft: DraftClauseReview,
) -> str:
    clause_text = clause if isinstance(clause, str) else clause
    if isinstance(clause_text, dict):
        import json

        clause_text = json.dumps(clause_text, ensure_ascii=False)
    return (
        "合同条款：\n"
        f"{clause_text}\n\n"
        "完整证据：\n"
        f"{_evidence_block(evidence)}\n\n"
        "生成结果：\n"
        f"{draft.model_dump_json()}\n\n"
        "请输出 JSON 对象，字段为 decision（pass/revise/reject）、"
        "unsupported_fields、evidence_gap、feedback。"
    )


class DeepSeekResultReviewer:
    def __init__(
        self,
        client: DeepSeekClient,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._model = model or client.review_model

    async def review(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
        draft: DraftClauseReview,
    ) -> ReviewDecision:
        messages = [
            {"role": "system", "content": _REVIEWER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _review_request(clause, evidence, draft),
            },
        ]
        return await self._client.complete_json(
            self._model, messages, ReviewDecision
        )
