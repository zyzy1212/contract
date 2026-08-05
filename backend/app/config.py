from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://contract:contract@localhost:5432/contract_review"
    redis_url: str = "redis://localhost:6379/0"
    object_store_endpoint: str = "http://localhost:9000"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_generation_model: str = "deepseek-v4-flash"
    deepseek_review_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float = Field(default=120.0, ge=1, le=600)
    deepseek_max_retries: int = Field(default=3, ge=1, le=10)
    deepseek_retry_base_delay_seconds: float = Field(
        default=1.0, ge=0, le=60
    )
    document_parser_max_source_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    document_parser_pdf_max_pages: int = Field(default=500, gt=0)
    document_parser_pdf_max_blocks: int = Field(default=20_000, gt=0)
    document_parser_pdf_max_lines: int = Field(default=100_000, gt=0)
    document_parser_pdf_max_characters: int = Field(default=5_000_000, gt=0)
    document_parser_docx_max_zip_members: int = Field(default=5_000, gt=0)
    document_parser_docx_max_expanded_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    document_parser_docx_max_member_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    document_parser_docx_max_compression_ratio: float = Field(default=100.0, ge=1.0)
    document_parser_docx_max_blocks: int = Field(default=20_000, gt=0)
    document_parser_docx_max_characters: int = Field(default=5_000_000, gt=0)
    knowledge_max_child_chunks: int = Field(default=5_000, gt=0)
    knowledge_max_total_embedding_elements: int = Field(default=5_120_000, gt=0)
    embedding_batch_size: int = Field(default=128, gt=0)
    embedding_precision: Literal["auto", "fp16", "bf16", "fp32"] = "auto"
    review_clause_concurrency: int = Field(default=4, ge=1, le=32)
    review_query_expansion_enabled: bool = True
    review_query_expansion_max_queries: int = Field(default=3, ge=1, le=8)
    review_query_expansion_min_characters: int = Field(default=30, ge=1, le=2048)
    review_retrieval_max_rounds: int = Field(default=3, ge=1, le=5)
    model_config = SettingsConfigDict(env_file=REPOSITORY_ROOT / ".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
