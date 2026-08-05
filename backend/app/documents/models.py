from pydantic import BaseModel, Field


class SourceLocator(BaseModel):
    page_start: int | None = None
    page_end: int | None = None
    article_number: str | None = Field(default=None, max_length=100)
    section_title: str | None = Field(default=None, max_length=500)
    paragraph_index: int | None = None
    bboxes: list[dict[str, float]] = Field(default_factory=list)


class ParsedBlock(BaseModel):
    block_id: str
    text: str
    block_type: str
    locator: SourceLocator


class ParsedDocument(BaseModel):
    filename: str
    sha256: str
    blocks: list[ParsedBlock]
