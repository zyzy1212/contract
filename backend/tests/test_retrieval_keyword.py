from app.knowledge.repository import _keyword_search_terms
from app.documents.structure import article_number_references


def test_prefers_whole_words_over_single_characters() -> None:
    text = "合同 合 同 金 金额 额 人民币 人 民 币 250 654"
    assert _keyword_search_terms(text) == [
        "合同",
        "金额",
        "人民币",
        "250",
        "654",
    ]


def test_falls_back_to_single_characters() -> None:
    assert _keyword_search_terms("合 同 金") == ["合", "同", "金"]


def test_extracts_article_number_references() -> None:
    assert article_number_references(
        "民法典 第五百一十一条 履行期限；第一百六十条 适用条件"
    ) == ["第五百一十一条", "第一百六十条"]


def test_article_number_references_normalize_spaces_and_deduplicate() -> None:
    assert article_number_references(
        "第 五百 一十一条 第五百一十一条"
    ) == ["第五百一十一条"]


def test_article_number_references_ignores_plain_text() -> None:
    assert article_number_references("合同履行期限不明确") == []
