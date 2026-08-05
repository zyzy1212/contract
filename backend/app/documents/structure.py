import re


_LEGAL_NUMBER = r"(?:\d+|[〇零一二三四五六七八九十百千万两]+)"
_ARTICLE_RE = re.compile(rf"^(第{_LEGAL_NUMBER}条(?:之{_LEGAL_NUMBER})?)")
_SECTION_RE = re.compile(
    rf"^第{_LEGAL_NUMBER}(?:分)?([编篇部章节])(.*)$"
)
_PART_RE = re.compile(rf"^第{_LEGAL_NUMBER}部分(.*)$")
_PROSE_PUNCTUATION = frozenset("。！？；：，,;:!?")
_PART_PROSE_PATTERN = re.compile(r"(?:说明|规定|约定|包括|是指|如下|正文)")
_SECTION_LEVELS = {"编": 1, "篇": 1, "部": 1, "章": 2, "节": 3}
_UNNUMBERED_SECTION_NAMES = frozenset(
    {"总则", "分则", "附则", "通则", "序言", "前言"}
)
_PAGE_NUMBER_RE = re.compile(
    r"^(?:"
    r"[-–—·•.]?\s*\d{1,4}\s*[-–—·•.]?"
    r"|[-–—·•.]?\s*\d{1,4}\s*[-–—]\s*\d{1,4}\s*[-–—·•.]?"
    r"|第\s*\d{1,4}\s*页"
    r"|第\s*\d{1,4}\s*[-–—]\s*\d{1,4}\s*页"
    r"|[-–—·•.]?\s*\d{1,4}\s*/\s*\d{1,4}\s*[-–—·•.]?"
    r"|page\s*\d{1,4}"
    r")$",
    re.IGNORECASE,
)
_TOC_ENTRY_RE = re.compile(
    r"(?:[\.．·•]{3,}|…{3,}|"
    r"^第[^。\n]{1,60}(?:部分|章|节|编|篇).{0,80}\s+\d{1,4}-\d{1,4}\s*$)"
)


def extract_article_number(text: str) -> str | None:
    match = _ARTICLE_RE.match(text.strip())
    return match.group(1) if match else None


def section_heading_level(text: str) -> int | None:
    candidate = text.strip()
    part_match = _PART_RE.match(candidate)
    if part_match:
        suffix = part_match.group(1)
        if not suffix:
            return 1
        title = suffix.strip()
        if (
            title
            and len(title) <= 40
            and not any(mark in title for mark in _PROSE_PUNCTUATION)
            and not _PART_PROSE_PATTERN.search(title)
        ):
            return 1
        return None
    match = _SECTION_RE.match(candidate)
    if not match:
        normalized = re.sub(r"\s+", "", candidate)
        if normalized in _UNNUMBERED_SECTION_NAMES:
            return 2
        return None

    suffix = match.group(2)
    if not suffix:
        return _SECTION_LEVELS[match.group(1)]
    if suffix[0].isspace():
        title = suffix.strip()
        if title and not any(mark in title for mark in _PROSE_PUNCTUATION):
            return _SECTION_LEVELS[match.group(1)]
        return None
    return None


def is_page_number_like(text: str) -> bool:
    """True when a line is a standalone page number rather than contract text."""
    return bool(_PAGE_NUMBER_RE.fullmatch(text.strip()))


def is_table_of_contents_entry(text: str) -> bool:
    """True when a line looks like a dotted TOC row or a TOC page reference."""
    return bool(_TOC_ENTRY_RE.search(text.strip()))
