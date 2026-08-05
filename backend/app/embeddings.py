from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EMBEDDING_PRECISIONS = frozenset({"auto", "fp16", "bf16", "fp32"})


def normalize_embedding_precision(precision: str) -> str:
    normalized = precision.strip().lower()
    if normalized not in EMBEDDING_PRECISIONS:
        raise ValueError(f"unsupported embedding precision: {precision}")
    return normalized


def load_sentence_transformer(
    model_name: str,
    precision: str = "auto",
) -> "SentenceTransformer":
    """Load a SentenceTransformer on CUDA when available, using fp16/bf16 on GPU."""
    import torch
    from sentence_transformers import SentenceTransformer

    normalized = normalize_embedding_precision(precision)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(model_name, device=device)
    if normalized == "fp32" or device == "cpu":
        if normalized in {"fp16", "bf16"} and device == "cpu":
            logger.warning(
                "precision %r requested but CUDA is unavailable; keeping fp32 on CPU",
                normalized,
            )
        return model
    if normalized in {"auto", "fp16"}:
        return model.half()
    return model.to(torch.bfloat16)
