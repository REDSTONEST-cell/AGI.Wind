from typing import Literal
from pathlib import Path
from pydantic import Field
from ..core.types import DTO
from ..archive.repository import digest

class EmbeddingSpace(DTO):
    model_name: str
    revision: str
    weights_hash: str
    tokenizer_hash: str
    dimension: int = Field(gt=0)
    normalize: bool = True
    metric: Literal["cosine", "l2", "inner_product"] = "cosine"
    preprocessing: str = "bge-zh-query-v1"

    @property
    def fingerprint(self): return digest(self.model_dump())

    def require_compatible(self, other):
        if self.fingerprint != other.fingerprint:
            raise ValueError("embedding fingerprint mismatch; rebuild a new space")


class EmbeddingService:
    """Injected local encoder; downloads and model selection are explicit setup operations."""
    def __init__(self, encoder, spaces): self.encoder, self.spaces = encoder, spaces
    async def embed(self, texts, space_id):
        space = self.spaces[space_id]
        space.require_compatible(self.encoder.space)
        vectors = self.encoder.encode(texts)
        if len(vectors) != len(texts) or any(len(row) != space.dimension for row in vectors):
            raise ValueError("invalid embedding dimensions")
        return vectors
