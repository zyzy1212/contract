from app.knowledge.ingestion import _child_parts


def test_article_header_keeps_full_body_start() -> None:
    text = (
        "第八百六十条　合作开发完成的发明创造，申请专利的权利属于"
        "合作开发的当事人共有；当事人一方转让其共有的专利申请权的，"
        "其他各方享有以同等条件优先受让的权利。但是，当事人另有约定的除外。"
    )
    children = list(_child_parts(text, 600))
    assert children == [text]
    assert "".join(children) == text


def test_contract_clause_starts_with_complete_word() -> None:
    text = (
        "第五百九十条　当事人一方因不可抗力不能履行合同的，根据不可抗力的影响，"
        "部分或者全部免除责任，但是法律另有规定的除外。因不可抗力不能履行合同的，"
        "应当及时通知对方，以减轻可能给对方造成的损失，并应当在合理期限内提供证明。"
    )
    children = list(_child_parts(text, 600))
    assert children == [text]
    assert children[0].startswith("第五百九十条　当事人")


def test_multiline_chunks_do_not_start_mid_word() -> None:
    first_line = (
        "第八百六十条　合作开发完成的发明创造，申请专利的权利属于"
        "合作开发的当事人共有；当事人一方转让其共有的专利申请权的，"
        "其他各方享有以同等条件优先受让的权利。但是，当事人另有约定的除外。"
    )
    repeated = (
        "合作开发的当事人一方声明放弃其共有的专利申请权的，除当事人另有约定外，"
        "可以由另一方单独申请或者由其他各方共同申请。申请人取得专利权的，"
        "放弃专利申请权的一方可以免费实施该专利。"
    )
    second_line = repeated * 4
    third_line = repeated * 4
    text = first_line + "\n" + second_line + "\n" + third_line
    children = list(_child_parts(text, 600))
    assert "".join(children) == text
    assert children[0].startswith("第八百六十条　合作")
    assert all(not child.startswith("作开发") for child in children)
    assert all(not child.startswith("事人一方") for child in children)
