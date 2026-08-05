from app.knowledge.repository import _keyword_search_terms


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
