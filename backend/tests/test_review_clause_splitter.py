from app.documents.models import ParsedBlock, ParsedDocument, SourceLocator
from app.tasks.review_contract import split_contract_clauses


def _block(text: str, *, index: int, block_type: str = "paragraph") -> ParsedBlock:
    return ParsedBlock(
        block_id=f"block-{index}",
        text=text,
        block_type=block_type,
        locator=SourceLocator(paragraph_index=index),
    )


def _document(blocks: list[ParsedBlock]) -> ParsedDocument:
    return ParsedDocument(
        filename="contract.pdf",
        sha256="0" * 64,
        blocks=blocks,
    )


def test_announcement_preamble_and_section_titles_are_skipped() -> None:
    parsed = _document(
        [
            _block("证券代码：002348 证券简称：高乐股份 公告编号：2018-002", index=0),
            _block("广东高乐玩具股份有限公司", index=1),
            _block("特别提示：", index=2),
            _block("一、交易对方基本情况", index=3),
            _block("1、采购方：普宁市教育局", index=4),
            _block("普宁市教育局隶属普宁市人民政府，为政府机构。", index=5),
            _block("二、合同主要内容", index=6),
            _block("1、项目名称：普宁市创建广东省教育现代化先进市配套设施设备建设项目", index=7),
            _block("2、合同金额：人民币250,654,002.40元。", index=8),
        ]
    )
    clauses = split_contract_clauses(parsed)
    texts = [clause.text for clause in clauses]
    assert not any("证券代码" in text for text in texts)
    assert not any("一、交易对方基本情况" in text for text in texts)
    assert not any("二、合同主要内容" in text for text in texts)
    assert (
        "1、采购方：普宁市教育局\n普宁市教育局隶属普宁市人民政府，为政府机构。"
        in texts
    )
    assert "2、合同金额：人民币250,654,002.40元。" in texts


def test_substantive_colon_headings_are_kept() -> None:
    parsed = _document(
        [
            _block("一、付款方式：合同签订后支付30%", index=0),
            _block("二、交货时间：2026年1月前", index=1),
        ]
    )
    clauses = split_contract_clauses(parsed)
    assert len(clauses) == 2
    assert clauses[0].text.startswith("一、付款方式")
    assert clauses[1].text.startswith("二、交货时间")


def test_plain_paragraphs_remain_one_clause_per_block() -> None:
    parsed = _document(
        [
            _block("甲方：张三", index=0),
            _block("乙方：李四", index=1),
        ]
    )
    clauses = split_contract_clauses(parsed)
    assert [clause.text for clause in clauses] == ["甲方：张三", "乙方：李四"]


def test_risk_and_impact_section_titles_are_skipped() -> None:
    parsed = _document(
        [
            _block("三、合同履行对公司的影响", index=0),
            _block("本项目有利于公司业务拓展。", index=1),
            _block("四、风险提示", index=2),
            _block("合同履行存在不可抗力风险。", index=3),
        ]
    )
    clauses = split_contract_clauses(parsed)
    texts = [clause.text for clause in clauses]
    assert not any("三、合同履行对公司的影响" in text for text in texts)
    assert not any("四、风险提示" in text for text in texts)
    assert "本项目有利于公司业务拓展。" in texts
    assert "合同履行存在不可抗力风险。" in texts


def test_fund_prospectus_section_headings_are_skipped() -> None:
    parsed = _document(
        [
            _block("一、基金份额的发售时间、发售方式、发售对象", index=0),
            _block("本基金自2026年8月6日起开始发售。", index=1),
            _block("二、基金份额的认购", index=2),
            _block("投资者应当在募集期内认购基金份额。", index=3),
        ]
    )
    clauses = split_contract_clauses(parsed)
    texts = [clause.text for clause in clauses]
    assert texts == [
        "本基金自2026年8月6日起开始发售。",
        "投资者应当在募集期内认购基金份额。",
    ]


def test_standalone_fund_section_headings_do_not_become_clauses() -> None:
    parsed = _document(
        [
            _block("一、基金份额的发售时间、发售方式、发售对象", index=0),
            _block("二、基金份额的认购", index=1),
        ]
    )
    clauses = split_contract_clauses(parsed)
    assert clauses == []


def test_closing_footer_blocks_are_skipped() -> None:
    parsed = _document(
        [
            _block("1、合同金额：人民币100万元。", index=0),
            _block("《采购合同》", index=1),
            _block("特此公告。", index=2),
            _block("广东高乐玩具股份有限公司", index=3),
            _block("董  事  会", index=4),
            _block("二○一八年一月三十日", index=5),
        ]
    )
    clauses = split_contract_clauses(parsed)
    texts = [clause.text for clause in clauses]
    assert texts == ["1、合同金额：人民币100万元。"]
