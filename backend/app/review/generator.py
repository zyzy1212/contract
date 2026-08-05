from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.llm.deepseek import DeepSeekClient
from app.review.schemas import GeneratedFinding
from app.retrieval.models import EvidenceCandidate


class DraftClauseReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    findings: list[GeneratedFinding] = Field(default_factory=list)


class FindingGenerator(Protocol):
    async def generate(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
    ) -> DraftClauseReview: ...

    async def revise(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
        draft: DraftClauseReview,
        feedback: str,
    ) -> DraftClauseReview: ...


_GENERATOR_SYSTEM_PROMPT = (
    "你是合同审核结果生成 Agent。只依据用户提供的合同条款和可用证据输出结构化审核意见。\n"
    "每项意见必须引用给出的证据 ID，只能使用用户列出的证据 ID。\n"
    "risk_level 只能是 high、medium、low 三选一，禁止使用其他值。\n"
    "禁止在 title、problem、reason、suggestion、proposed_clause 中输出证据原文不存在的数字或条款编号。\n"
    "不得引入证据中没有的金额、比例、期限、主体义务或法律结论。\n"
    "不输出最终法律建议，不把模型错误当作普通文本返回。\n"
    "必须使用 JSON 输出。"
)


def _render_clause(clause: Mapping[str, Any] | str) -> str:
    if isinstance(clause, str):
        return clause
    return json.dumps(clause, ensure_ascii=False)


def _evidence_block(evidence: Sequence[EvidenceCandidate]) -> str:
    return "\n".join(f"[{item.chunk_id}] {item.text}" for item in evidence)


def _generation_request(
    clause: Mapping[str, Any] | str,
    evidence: Sequence[EvidenceCandidate],
) -> str:
    return (
        "合同条款：\n"
        f"{_render_clause(clause)}\n\n"
        "可用证据：\n"
        f"{_evidence_block(evidence)}\n\n"
        "请输出 JSON 对象，字段 findings 是数组；"
        "每个 finding 必须包含 title、risk_level、problem、reason、suggestion、"
        "proposed_clause、evidence_ids。"
    )


def _revision_request(
    clause: Mapping[str, Any] | str,
    evidence: Sequence[EvidenceCandidate],
    draft: DraftClauseReview,
    feedback: str,
) -> str:
    return (
        "合同条款：\n"
        f"{_render_clause(clause)}\n\n"
        "可用证据：\n"
        f"{_evidence_block(evidence)}\n\n"
        "复审意见：\n"
        f"{feedback}\n\n"
        "当前草稿：\n"
        f"{draft.model_dump_json()}\n\n"
        "请按复审意见修订后重新输出同样的 JSON 结构。"
    )


class DeepSeekFindingGenerator:
    def __init__(
        self,
        client: DeepSeekClient,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._model = model or client.generation_model

    async def generate(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
    ) -> DraftClauseReview:
        messages = [
            {"role": "system", "content": _GENERATOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _generation_request(clause, evidence),
            },
        ]
        return await self._client.complete_json(
            self._model, messages, DraftClauseReview
        )

    async def revise(
        self,
        clause: Mapping[str, Any] | str,
        evidence: Sequence[EvidenceCandidate],
        draft: DraftClauseReview,
        feedback: str,
    ) -> DraftClauseReview:
        messages = [
            {"role": "system", "content": _GENERATOR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _revision_request(
                    clause, evidence, draft, feedback
                ),
            },
        ]
        return await self._client.complete_json(
            self._model, messages, DraftClauseReview
        )
