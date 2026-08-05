import hashlib
from pathlib import Path

import fitz

from app.config import Settings, get_settings

from .models import ParsedBlock, ParsedDocument, SourceLocator
from .resources import validate_and_hash_source
from .structure import (
    extract_article_number,
    is_page_number_like,
    is_table_of_contents_entry,
    section_heading_level,
)


def _classification_text(text: str) -> str:
    return text.strip()


def _normalize_margin_text(text: str) -> str:
    return "".join(text.split())


def _is_margin_line(bbox: dict[str, float], page_height: float) -> bool:
    return bbox["top"] <= page_height * 0.15 or bbox["bottom"] >= page_height * 0.85


def _repeated_margin_texts(document: fitz.Document) -> set[str]:
    counts: dict[str, int] = {}
    for page in document:
        page_height = page.rect.height
        for block in page.get_text("dict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                if not _classification_text(text) or is_page_number_like(text):
                    continue
                bbox = dict(zip(("x0", "top", "x1", "bottom"), line["bbox"]))
                if not _is_margin_line(bbox, page_height):
                    continue
                normalized = _normalize_margin_text(text)
                if len(normalized) >= 4:
                    counts[normalized] = counts.get(normalized, 0) + 1
    return {text for text, count in counts.items() if count >= 2}


def _text_lines(
    block: dict,
    *,
    page_height: float,
    repeated_margin_texts: set[str],
) -> list[tuple[str, dict[str, float]]]:
    lines: list[tuple[str, dict[str, float]]] = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        if not _classification_text(text):
            continue
        if is_page_number_like(text):
            continue
        if is_table_of_contents_entry(text):
            continue
        x0, top, x1, bottom = line["bbox"]
        bbox = {"x0": float(x0), "top": float(top), "x1": float(x1), "bottom": float(bottom)}
        if (
            _is_margin_line(bbox, page_height)
            and _normalize_margin_text(text) in repeated_margin_texts
        ):
            continue
        lines.append(
            (
                text,
                bbox,
            )
        )
    return lines


def _structural_segments(
    lines: list[tuple[str, dict[str, float]]],
) -> list[tuple[str, list[dict[str, float]]]]:
    segments: list[tuple[str, list[dict[str, float]]]] = []
    current_lines: list[str] = []
    current_bboxes: list[dict[str, float]] = []

    def flush() -> None:
        if current_lines:
            segments.append(("\n".join(current_lines), list(current_bboxes)))
            current_lines.clear()
            current_bboxes.clear()

    for text, bbox in lines:
        classification_text = _classification_text(text)
        if section_heading_level(classification_text) is not None:
            flush()
            segments.append((text, [bbox]))
        elif extract_article_number(classification_text) is not None:
            flush()
            current_lines.append(text)
            current_bboxes.append(bbox)
        else:
            current_lines.append(text)
            current_bboxes.append(bbox)
    flush()
    return segments


def _block_id(document_hash: str, locator: SourceLocator, block_type: str) -> str:
    structural_key = (
        f"{document_hash}|pdf|page:{locator.page_start}|"
        f"paragraph:{locator.paragraph_index}|type:{block_type}"
    )
    return hashlib.sha256(structural_key.encode("utf-8")).hexdigest()


def _enforce_pdf_limit(value: int, limit: int, label: str) -> None:
    if value > limit:
        raise ValueError(f"PDF exceeds configured {label} limit of {limit}")


def _make_block(
    document_hash: str,
    page_index: int,
    paragraph_index: int,
    text: str,
    bboxes: list[dict[str, float]],
    *,
    section_title: str | None,
    current_article: str | None,
    block_type: str,
) -> ParsedBlock:
    locator = SourceLocator(
        page_start=page_index,
        page_end=page_index,
        article_number=current_article,
        section_title=section_title,
        paragraph_index=paragraph_index,
        bboxes=bboxes,
    )
    return ParsedBlock(
        block_id=_block_id(document_hash, locator, block_type),
        text=text,
        block_type=block_type,
        locator=locator,
    )


def parse_pdf(path: Path, *, settings: Settings | None = None) -> ParsedDocument:
    path = Path(path)
    settings = settings or get_settings()
    document_hash = validate_and_hash_source(
        path, settings.document_parser_max_source_bytes
    )
    blocks: list[ParsedBlock] = []
    section_title: str | None = None
    section_stack: list[tuple[int, str]] = []
    extracted_block_count = 0
    extracted_line_count = 0
    extracted_character_count = 0

    try:
        document = fitz.open(path)
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise ValueError(f"invalid or corrupted PDF: {path}") from exc

    with document:
        if document.needs_pass:
            raise PermissionError(f"encrypted PDF requires a password: {path}")
        _enforce_pdf_limit(
            document.page_count,
            settings.document_parser_pdf_max_pages,
            "page count",
        )
        repeated_margin_texts = _repeated_margin_texts(document)
        in_toc = False
        toc_page: int | None = None

        for page_index, page in enumerate(document, start=1):
            paragraph_index = 0
            current_article: str | None = None
            page_height = page.rect.height
            for raw_block in page.get_text("dict", sort=True).get("blocks", []):
                extracted_block_count += 1
                _enforce_pdf_limit(
                    extracted_block_count,
                    settings.document_parser_pdf_max_blocks,
                    "block count",
                )
                if raw_block.get("type") != 0:
                    continue
                raw_lines = raw_block.get("lines", [])
                extracted_line_count += len(raw_lines)
                _enforce_pdf_limit(
                    extracted_line_count,
                    settings.document_parser_pdf_max_lines,
                    "line count",
                )
                extracted_character_count += sum(
                    len(span.get("text", ""))
                    for line in raw_lines
                    for span in line.get("spans", [])
                )
                _enforce_pdf_limit(
                    extracted_character_count,
                    settings.document_parser_pdf_max_characters,
                    "character count",
                )
                segments = _structural_segments(
                    _text_lines(
                        raw_block,
                        page_height=page_height,
                        repeated_margin_texts=repeated_margin_texts,
                    )
                )
                for text, bboxes in segments:
                    _enforce_pdf_limit(
                        len(blocks) + 1,
                        settings.document_parser_pdf_max_blocks,
                        "block count",
                    )
                    classification_text = _classification_text(text)
                    if in_toc:
                        if page_index == toc_page or is_table_of_contents_entry(text):
                            continue
                        in_toc = False
                    if _normalize_margin_text(classification_text) == "目录":
                        blocks.clear()
                        in_toc = True
                        toc_page = page_index
                        continue
                    paragraph_index += 1
                    is_section = section_heading_level(classification_text) is not None
                    if is_section:
                        level = section_heading_level(classification_text) or 1
                        section_stack = [
                            item
                            for item in section_stack
                            if item[0] < level
                        ]
                        section_stack.append((level, text))
                        section_title = " > ".join(
                            item[1] for item in section_stack
                        )
                        current_article = None
                    article_number = extract_article_number(classification_text)
                    if article_number is not None:
                        current_article = article_number
                    block_type = "heading" if is_section else "article" if article_number else "paragraph"
                    blocks.append(
                        _make_block(
                            document_hash,
                            page_index,
                            paragraph_index,
                            text,
                            bboxes,
                            section_title=section_title,
                            current_article=current_article,
                            block_type=block_type,
                        )
                    )

    if not blocks:
        raise ValueError("PDF contains no usable text; OCR is not available")

    return ParsedDocument(filename=path.name, sha256=document_hash, blocks=blocks)
