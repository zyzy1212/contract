from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Mapping, Protocol

from app.auth import Actor
from app.retrieval.models import EvidenceCandidate, TransientRetrievalError
from app.review.evidence_judge import EvidenceDecision, EvidenceJudge
from app.review.generator import DraftClauseReview, FindingGenerator
from app.review.query_expansion import QueryExpander
from app.review.reviewer import ResultReviewer
from app.review.schemas import EvidenceCollection, FinalClauseReview, GeneratedFinding


class EvidenceRetriever(Protocol):
    async def search(
        self,
        actor: Actor,
        query: str,
        excluded_chunk_ids: Sequence[str] = (),
    ) -> list[EvidenceCandidate]: ...


_DATE_PATTERNS = (
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),
)
_PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_LIST_MARKER_FOLLOWERS = ".、)]）"
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_PLACE_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_SECTION_UNITS = {"万": 10_000, "亿": 100_000_000}
_CN_NUMERAL_RE = re.compile(r"[零〇一二三四五六七八九十百千万两]+")
MAX_EXPANDED_QUERIES = 8
MAX_EXPANDED_CANDIDATES = 20


def _chinese_numeral_to_int(value: str) -> int | None:
    if not value:
        return None
    if not any(char in "十百千万亿" for char in value):
        digits = "".join(str(_CN_DIGITS[char]) for char in value)
        return int(digits) if digits else None
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_PLACE_UNITS:
            section += (number or 1) * _CN_PLACE_UNITS[char]
            number = 0
        elif char in _CN_SECTION_UNITS:
            section = (section + number) * _CN_SECTION_UNITS[char]
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def _normalize_cn_numbers(text: str) -> str:
    def replace(match: re.Match) -> str:
        value = _chinese_numeral_to_int(match.group(0))
        return str(value) if value is not None else match.group(0)

    return _CN_NUMERAL_RE.sub(replace, text)


def _grounding_value_matches(evidence_text: str, value: str) -> bool:
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return re.search(
            rf"(?<!\d){re.escape(value)}(?!\d)", evidence_text
        ) is not None
    return value in evidence_text


def _is_list_marker(text: str, match: re.Match) -> bool:
    end = match.end()
    if end >= len(text):
        return False
    follower = text[end]
    if follower not in _LIST_MARKER_FOLLOWERS:
        return False
    if follower == "." and end + 1 < len(text) and text[end + 1].isdigit():
        return False
    return True


def _grounding_values(finding: GeneratedFinding) -> list[str]:
    text = " ".join(
        (
            finding.title,
            finding.problem,
            finding.reason,
            finding.suggestion,
            finding.proposed_clause,
        )
    )
    values: list[str] = []
    seen: set[str] = set()
    for pattern in (*_DATE_PATTERNS, _PERCENT_PATTERN, _NUMBER_PATTERN):
        for match in pattern.finditer(text):
            value = match.group(0).replace(" ", "")
            if not value or value in seen:
                continue
            if pattern is _NUMBER_PATTERN and _is_list_marker(text, match):
                continue
            seen.add(value)
            values.append(value)
    return values


def validate_grounding(
    finding: GeneratedFinding,
    allowed_evidence_ids: set[str],
    *,
    evidence_by_id: Mapping[str, str] | None = None,
) -> None:
    """Reject findings that cite unknown evidence or unsupported deterministic values."""

    unknown = set(finding.evidence_ids) - allowed_evidence_ids
    if unknown:
        raise ValueError(f"unknown evidence ids: {sorted(unknown)}")
    if evidence_by_id is None:
        return
    evidence_text = _normalize_cn_numbers(
        "\n".join(
            evidence_by_id.get(evidence_id, "")
            for evidence_id in finding.evidence_ids
        ).replace(" ", "")
    )
    for value in _grounding_values(finding):
        normalized_value = _normalize_cn_numbers(value.replace(" ", ""))
        if not _grounding_value_matches(evidence_text, normalized_value):
            raise ValueError(f"finding contains unsupported value: {value}")


async def generate_and_review(
    clause: Mapping[str, Any] | str,
    evidence: Sequence[EvidenceCandidate],
    generator: FindingGenerator,
    reviewer: ResultReviewer,
    max_revisions: int = 2,
) -> FinalClauseReview:
    if max_revisions < 0:
        raise ValueError("max_revisions must not be negative")
    allowed = {item.chunk_id for item in evidence}
    evidence_by_id = {item.chunk_id: item.text for item in evidence}
    draft = await generator.generate(clause, evidence)
    for revision in range(max_revisions + 1):
        for finding in draft.findings:
            validate_grounding(finding, allowed, evidence_by_id=evidence_by_id)
        decision = await reviewer.review(clause, evidence, draft)
        if decision.decision == "pass":
            return FinalClauseReview(
                status="complete",
                findings=draft.findings,
                review_decision=decision,
            )
        if decision.evidence_gap:
            return FinalClauseReview(
                status="needs_retrieval",
                findings=[],
                review_decision=decision,
            )
        if revision == max_revisions or decision.decision == "reject":
            return FinalClauseReview(
                status="review_failed",
                findings=[],
                review_decision=decision,
            )
        draft = await generator.revise(
            clause, evidence, draft, decision.feedback
        )
    raise AssertionError("review loop exceeded its bounded revisions")


def _validate_judge_evidence(
    decision: EvidenceDecision,
    allowed_evidence_ids: set[str],
) -> None:
    unknown = sorted(set(decision.usable_evidence_ids) - allowed_evidence_ids)
    if unknown:
        raise ValueError(f"judge selected unknown evidence ids: {unknown}")


async def collect_sufficient_evidence(
    actor: Actor,
    initial_query: str,
    retriever: EvidenceRetriever,
    judge: EvidenceJudge,
    max_rounds: int = 3,
    query_expander: QueryExpander | None = None,
) -> EvidenceCollection:
    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    query = initial_query.strip()
    if not query:
        raise ValueError("initial_query must not be empty")
    seen: set[str] = set()
    seen_candidates: list[EvidenceCandidate] = []
    trace: list[dict] = []
    for round_number in range(1, max_rounds + 1):
        search_queries = [query]
        if round_number == 1 and query_expander is not None:
            try:
                expanded = await query_expander.expand(query)
                search_queries = list(
                    dict.fromkeys([query, *expanded])
                )[:MAX_EXPANDED_QUERIES]
            except Exception:
                search_queries = [query]
        merged: dict[str, EvidenceCandidate] = {}
        for search_query in search_queries:
            try:
                candidates = await retriever.search(
                    actor,
                    search_query,
                    excluded_chunk_ids=sorted(seen),
                )
            except TransientRetrievalError:
                candidates = await retriever.search(
                    actor,
                    search_query,
                    excluded_chunk_ids=sorted(seen),
                )
            for item in candidates:
                if item.chunk_id not in seen and item.chunk_id not in merged:
                    merged[item.chunk_id] = item
        candidates = list(merged.values())[:MAX_EXPANDED_CANDIDATES]
        candidate_ids = [item.chunk_id for item in candidates]
        seen_candidates.extend(candidates)
        seen.update(candidate_ids)
        decision = await judge.evaluate(query, candidates)
        _validate_judge_evidence(decision, seen)
        round_trace = {
            "round": round_number,
            "query": query,
            "candidate_ids": candidate_ids,
            "decision": decision.model_dump(),
        }
        if len(search_queries) > 1:
            round_trace["queries"] = search_queries
        trace.append(round_trace)
        if decision.sufficient:
            candidate_by_id = {item.chunk_id: item for item in seen_candidates}
            return EvidenceCollection(
                status="sufficient",
                rounds=round_number,
                evidence_ids=list(dict.fromkeys(decision.usable_evidence_ids)),
                trace=trace,
                candidates=[
                    candidate_by_id[evidence_id]
                    for evidence_id in decision.usable_evidence_ids
                    if evidence_id in candidate_by_id
                ],
            )
        next_query = "；".join(decision.follow_up_queries) or "；".join(
            decision.missing_points
        )
        if not next_query.strip():
            return EvidenceCollection(
                status="insufficient",
                rounds=round_number,
                trace=trace,
            )
        query = next_query
    return EvidenceCollection(
        status="insufficient",
        rounds=max_rounds,
        trace=trace,
    )
