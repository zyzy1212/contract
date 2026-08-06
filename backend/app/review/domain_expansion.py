"""Deterministic domain-aware query expansion for Chinese contract review.

LLM expansion is generic and can miss the exact legal provisions that govern a
clause. These rules append focused queries that include the relevant statute
and article numbers, which the retrieval layer matches exactly.
"""

from __future__ import annotations

from typing import Protocol


_DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "联合体",
        (
            "联合体 共同投标协议 明确约定各方拟承担的工作和责任 连带责任 "
            "政府采购法 第二十四条",
            "联合体 共同投标协议 明确约定各方拟承担的工作和责任 连带责任 "
            "招标投标法 第三十一条",
        ),
    ),
    (
        "生效",
        (
            "合同 成立 生效 签字 盖章 按指印 民法典 第四百九十条 第五百零二条",
        ),
    ),
    (
        "履行期限",
        (
            "合同 履行期限 约定不明确 协议补充 交易习惯 民法典 第五百一十条",
            "合同 履行期限 不明确 随时履行 必要准备时间 民法典 第五百一十一条",
            "迟延履行 违约责任 继续履行 赔偿损失 民法典 第五百七十七条",
        ),
    ),
    (
        "不可抗力",
        (
            "不可抗力 免责 通知义务 合理期限 证明 民法典 第一百八十条 第五百九十条",
        ),
    ),
    (
        "违约",
        (
            "违约责任 违约金 继续履行 采取补救措施 赔偿损失 民法典 第五百七十七条 "
            "第五百八十五条",
        ),
    ),
    (
        "付款",
        (
            "价款 报酬 支付 约定不明确 履行地 市场价格 民法典 第五百一十条 "
            "第五百一十一条",
        ),
    ),
    (
        "争议",
        (
            "合同纠纷 诉讼 仲裁 管辖 争议解决方式 民事诉讼法 第三十四条",
        ),
    ),
    (
        "质量",
        (
            "质量标准 约定不明确 强制性国家标准 推荐性国家标准 行业标准 民法典 "
            "第五百一十一条",
        ),
    ),
)


class DomainQueryExpander:
    """Return focused legal queries for clauses containing known risk terms."""

    def expand(self, query: str) -> list[str]:
        text = query.strip()
        if not text:
            return []
        queries: list[str] = []
        for keyword, candidates in _DOMAIN_RULES:
            if keyword in text:
                for candidate in candidates:
                    if candidate not in queries:
                        queries.append(candidate)
        return queries


class QueryExpander(Protocol):
    async def expand(self, query: str) -> list[str]: ...


class CombinedQueryExpander:
    """Merge LLM expansion with deterministic domain rules."""

    def __init__(
        self,
        *,
        llm_expander: QueryExpander | None = None,
        domain_expander: DomainQueryExpander | None = None,
    ) -> None:
        self._llm_expander = llm_expander
        self._domain_expander = domain_expander or DomainQueryExpander()

    async def expand(self, query: str) -> list[str]:
        combined: list[str] = []
        if self._llm_expander is not None:
            try:
                combined.extend(await self._llm_expander.expand(query))
            except Exception:
                combined = []
        combined.extend(self._domain_expander.expand(query))
        original = query.strip()
        seen: list[str] = []
        for candidate in combined:
            candidate = candidate.strip()
            if candidate and candidate != original and candidate not in seen:
                seen.append(candidate)
        return seen[:8]
