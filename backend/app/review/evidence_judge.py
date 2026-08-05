from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.llm.deepseek import DeepSeekClient
from app.retrieval.models import EvidenceCandidate


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sufficient: bool
    missing_points: list[str] = Field(default_factory=list)
    follow_up_queries: list[str] = Field(default_factory=list)
    usable_evidence_ids: list[str] = Field(default_factory=list)
    rejected_evidence: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "missing_points",
        "follow_up_queries",
        "usable_evidence_ids",
        mode="before",
    )
    @classmethod
    def _clean_string_lists(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        cleaned = [str(item).strip() for item in value]
        return [item for item in cleaned if item]

    @field_validator("rejected_evidence", mode="before")
    @classmethod
    def _normalize_rejected_evidence(cls, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, (list, tuple, set)):
            return {
                str(item).strip(): "该证据与本条款审核不直接相关"
                for item in value
                if str(item).strip()
            }
        raise ValueError("rejected_evidence must be a mapping or list of evidence ids")

    @model_validator(mode="after")
    def require_usable_evidence_when_sufficient(self):
        if self.sufficient and not self.usable_evidence_ids:
            raise ValueError("sufficient evidence requires usable evidence ids")
        return self

    @model_validator(mode="after")
    def require_reasons_for_rejected_evidence(self):
        empty = [
            evidence_id
            for evidence_id, reason in self.rejected_evidence.items()
            if not reason.strip()
        ]
        if empty:
            raise ValueError("rejected evidence requires a reason")
        return self


class EvidenceJudge(Protocol):
    async def evaluate(
        self,
        query: str,
        candidates: Sequence[EvidenceCandidate],
    ) -> EvidenceDecision: ...


_JUDGE_SYSTEM_PROMPT = (
    "你是证据充分性判断 Agent。只输出结构化判断，不生成最终审核意见。\n"
    "判断候选证据能否支撑合同条款审核；证据不足时给出缺失要点和下一轮检索词。\n"
    "只能选择用户给出的证据编号（如 E1、E2）；拒绝的证据必须给出原因。\n"
    "sufficient 必须是 JSON 布尔值；missing_points、follow_up_queries、"
    "usable_evidence_ids 必须是字符串数组；rejected_evidence 必须是对象。\n"
    "不输出最终法律建议。必须使用 JSON 输出。"
)


def _evidence_aliases(
    candidates: Sequence[EvidenceCandidate],
) -> dict[str, str]:
    return {
        f"E{index}": item.chunk_id
        for index, item in enumerate(candidates, start=1)
    }


def _resolve_decision_ids(
    decision: EvidenceDecision,
    aliases: dict[str, str],
) -> EvidenceDecision:
    if not aliases:
        return decision
    by_alias = {alias: chunk_id for alias, chunk_id in aliases.items()}
    usable = [
        by_alias.get(evidence_id, evidence_id)
        for evidence_id in decision.usable_evidence_ids
    ]
    rejected = {
        by_alias.get(evidence_id, evidence_id): reason
        for evidence_id, reason in decision.rejected_evidence.items()
    }
    return decision.model_copy(
        update={"usable_evidence_ids": usable, "rejected_evidence": rejected}
    )


def _judge_request(
    query: str,
    candidates: Sequence[EvidenceCandidate],
) -> str:
    aliases = _evidence_aliases(candidates)
    candidate_block = "\n".join(
        f"[{alias}] {item.text}"
        for alias, item in zip(aliases, candidates, strict=True)
    )
    return (
        "待审核条款检索词：\n"
        f"{query}\n\n"
        "候选证据：\n"
        f"{candidate_block}\n\n"
        "请输出 JSON 对象，字段为 sufficient、missing_points、"
        "follow_up_queries、usable_evidence_ids、rejected_evidence。"
    )


class DeepSeekEvidenceJudge:
    def __init__(
        self,
        client: DeepSeekClient,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._model = model or client.generation_model

    async def evaluate(
        self,
        query: str,
        candidates: Sequence[EvidenceCandidate],
    ) -> EvidenceDecision:
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _judge_request(query, candidates)},
        ]
        decision = await self._client.complete_json(
            self._model, messages, EvidenceDecision
        )
        return _resolve_decision_ids(decision, _evidence_aliases(candidates))
