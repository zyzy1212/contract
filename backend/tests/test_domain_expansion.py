from app.review.domain_expansion import DomainQueryExpander


def test_union_body_clause_expands_to_law_queries() -> None:
    queries = DomainQueryExpander().expand(
        "2、联合体成员：中国电信股份有限公司广东分公司"
    )
    assert any("第二十四条" in query for query in queries)
    assert any("第三十一条" in query for query in queries)


def test_effective_clause_expands_to_contract_formation_rules() -> None:
    queries = DomainQueryExpander().expand("合同于各方签字盖章后生效。")
    assert any("第四百九十条" in query for query in queries)
    assert any("第五百零二条" in query for query in queries)


def test_performance_period_clause_expands_to_deadline_rules() -> None:
    queries = DomainQueryExpander().expand(
        "合同履行期限：截至2018年7月30日前完成施工。"
    )
    assert any("第五百一十条" in query for query in queries)
    assert any("第五百一十一条" in query for query in queries)


def test_plain_clause_has_no_domain_queries() -> None:
    assert DomainQueryExpander().expand("项目名称为教育设备建设项目。") == []
