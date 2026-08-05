from pathlib import Path

from .docx_parser import parse_docx
from .models import ParsedDocument
from .pdf_parser import parse_pdf


def parse_document(path: Path) -> ParsedDocument:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"document does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"document path is not a file: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    raise ValueError("only PDF and DOCX are supported")
