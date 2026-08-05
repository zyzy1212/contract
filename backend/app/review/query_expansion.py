from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings
from app.llm.deepseek import DeepSeekClient


MAX_EXPANDED_QUERY_LENGTH = 512


class QueryExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    queries: list[str] = Field(default_factory=list)

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            query = str(item).strip()
            if not query or len(query) > MAX_EXPANDED_QUERY_LENGTH:
                continue
            if query not in cleaned:
                cleaned.append(query)
        return cleaned[:8]

    @model_validator(mode="after")
    def _require_queries(self) -> "QueryExpansion":
        if not self.queries:
            raise ValueError("queries must not be empty")
        return self


class QueryExpander(Protocol):
    async def expand(self, query: str) -> list[str]: ...


_EXPANSION_SYSTEM_PROMPT = (
    "你是合同审核知识库检索词扩写 Agent。\n"
    "根据合同条款生成用于法规知识库检索的查询词，不生成最终审核意见。\n"
    "查询词必须忠实于条款语义，可以补充同义表述、法律术语、监管口径和缺失要点；\n"
    "不得虚构条款中没有的事实、金额、期限或法规名称。\n"
    "每条查询词必须能独立用于检索，长度不超过 512 字。\n"
    "必须使用 JSON 输出，字段为 queries，值是字符串数组。"
)


def _expansion_request(query: str, max_queries: int) -> str:
    return (
        "合同条款（或当前检索词）：\n"
        f"{query}\n\n"
        f"请生成不超过 {max_queries} 条检索查询词，覆盖不同表述和检索角度。"
    )


class DeepSeekQueryExpander:
    def __init__(
        self,
        client: DeepSeekClient,
        *,
        model: str | None = None,
        max_queries: int | None = None,
        min_characters: int | None = None,
    ) -> None:
        self._client = client
        self._model = model or client.generation_model
        if max_queries is None:
            max_queries = get_settings().review_query_expansion_max_queries
        if not 1 <= max_queries <= 8:
            raise ValueError("max_queries must be between 1 and 8")
        self._max_queries = max_queries
        if min_characters is None:
            min_characters = get_settings().review_query_expansion_min_characters
        if min_characters < 1:
            raise ValueError("min_characters must be positive")
        self._min_characters = min_characters

    async def expand(self, query: str) -> list[str]:
        if len(query.strip()) < self._min_characters:
            return []
        messages = [
            {"role": "system", "content": _EXPANSION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _expansion_request(query, self._max_queries),
            },
        ]
        expansion = await self._client.complete_json(
            self._model,
            messages,
            QueryExpansion,
            temperature=0.2,
            max_tokens=600,
        )
        original = query.strip()
        return [
            candidate
            for candidate in expansion.queries
            if candidate != original
        ][: self._max_queries]
