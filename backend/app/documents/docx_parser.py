import hashlib
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from app.config import Settings, get_settings

from .models import ParsedBlock, ParsedDocument, SourceLocator
from .resources import validate_and_hash_source
from .structure import extract_article_number, section_heading_level


_HEADING_LEVEL_RE = re.compile(r"heading\s*([1-9])", re.IGNORECASE)


def _classification_text(text: str) -> str:
    return text.strip()


def _heading_level(paragraph: Paragraph) -> int | None:
    text_level = section_heading_level(paragraph.text)
    if text_level is not None:
        return text_level
    style = paragraph.style
    names = (getattr(style, "name", "") or "", getattr(style, "style_id", "") or "")
    for name in names:
        match = _HEADING_LEVEL_RE.search(name)
        if match:
            return int(match.group(1))
    return section_heading_level(paragraph.text)


def _is_list(paragraph: Paragraph) -> bool:
    style = paragraph.style
    names = (getattr(style, "name", "") or "", getattr(style, "style_id", "") or "")
    if any("list" in name.lower() for name in names):
        return True
    paragraph_properties = paragraph._p.pPr
    return paragraph_properties is not None and paragraph_properties.numPr is not None


def _block_id(document_hash: str, structural_locator: str, block_type: str) -> str:
    key = f"{document_hash}|docx|{structural_locator}|type:{block_type}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _table_text(table: Table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        rows.append([cell.text for cell in row.cells])
    if not any(_classification_text(cell) for row in rows for cell in row):
        return ""
    return "\n".join("\t".join(row) for row in rows)


def _inspect_docx_package(path: Path, settings: Settings) -> None:
    try:
        with ZipFile(path) as package:
            members = package.infolist()
            if len(members) > settings.document_parser_docx_max_zip_members:
                raise ValueError(
                    "DOCX exceeds configured ZIP member count limit of "
                    f"{settings.document_parser_docx_max_zip_members}"
                )

            expanded_size = 0
            for member in members:
                if member.file_size > settings.document_parser_docx_max_member_bytes:
                    raise ValueError(
                        "DOCX exceeds configured member size limit of "
                        f"{settings.document_parser_docx_max_member_bytes} bytes"
                    )
                expanded_size += member.file_size
                if expanded_size > settings.document_parser_docx_max_expanded_bytes:
                    raise ValueError(
                        "DOCX exceeds configured expanded size limit of "
                        f"{settings.document_parser_docx_max_expanded_bytes} bytes"
                    )
                if member.file_size:
                    ratio = member.file_size / max(member.compress_size, 1)
                    if ratio > settings.document_parser_docx_max_compression_ratio:
                        raise ValueError(
                            "DOCX exceeds configured compression ratio limit of "
                            f"{settings.document_parser_docx_max_compression_ratio:g}"
                        )
    except BadZipFile as exc:
        raise ValueError(f"invalid or corrupted DOCX: {path}") from exc


def _enforce_docx_output_limit(
    blocks: list[ParsedBlock], total_characters: int, text: str, settings: Settings
) -> int:
    if len(blocks) + 1 > settings.document_parser_docx_max_blocks:
        raise ValueError(
            "DOCX exceeds configured block count limit of "
            f"{settings.document_parser_docx_max_blocks}"
        )
    next_total = total_characters + len(text)
    if next_total > settings.document_parser_docx_max_characters:
        raise ValueError(
            "DOCX exceeds configured character count limit of "
            f"{settings.document_parser_docx_max_characters}"
        )
    return next_total


def parse_docx(path: Path, *, settings: Settings | None = None) -> ParsedDocument:
    path = Path(path)
    settings = settings or get_settings()
    document_hash = validate_and_hash_source(
        path, settings.document_parser_max_source_bytes
    )
    _inspect_docx_package(path, settings)
    try:
        document = Document(path)
    except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError, ValueError) as exc:
        raise ValueError(f"invalid or corrupted DOCX: {path}") from exc
    if document.element.tag != qn("w:document") or document.element.body is None:
        raise ValueError(f"invalid or corrupted DOCX: {path}")

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_index = 0
    table_index = 0
    total_characters = 0
    current_article: str | None = None

    for element in document.element.body.iterchildren():
        if isinstance(element, CT_P):
            paragraph_index += 1
            paragraph = Paragraph(element, document)
            text = paragraph.text
            classification_text = _classification_text(text)
            if not classification_text:
                continue

            heading_level = _heading_level(paragraph)
            if heading_level is not None:
                heading_stack = [item for item in heading_stack if item[0] < heading_level]
                heading_stack.append((heading_level, text))
                current_article = None
            section_title = " > ".join(item[1] for item in heading_stack) or None
            article_number = extract_article_number(classification_text)
            if article_number is not None:
                current_article = article_number
            if heading_level is not None:
                block_type = "heading"
            elif article_number:
                block_type = "article"
            elif _is_list(paragraph):
                block_type = "list"
            else:
                block_type = "paragraph"

            locator = SourceLocator(
                article_number=current_article,
                section_title=section_title,
                paragraph_index=paragraph_index,
            )
            total_characters = _enforce_docx_output_limit(
                blocks, total_characters, text, settings
            )
            blocks.append(
                ParsedBlock(
                    block_id=_block_id(document_hash, f"paragraph:{paragraph_index}", block_type),
                    text=text,
                    block_type=block_type,
                    locator=locator,
                )
            )
        elif isinstance(element, CT_Tbl):
            table_index += 1
            table = Table(element, document)
            text = _table_text(table)
            if not text:
                continue
            locator = SourceLocator(
                article_number=current_article,
                section_title=" > ".join(item[1] for item in heading_stack) or None,
                paragraph_index=table_index,
            )
            total_characters = _enforce_docx_output_limit(
                blocks, total_characters, text, settings
            )
            blocks.append(
                ParsedBlock(
                    block_id=_block_id(document_hash, f"table:{table_index}", "table"),
                    text=text,
                    block_type="table",
                    locator=locator,
                )
            )

    if not blocks:
        raise ValueError("DOCX contains no usable text")

    return ParsedDocument(filename=path.name, sha256=document_hash, blocks=blocks)
