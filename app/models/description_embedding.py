"""Cached vectors for transaction descriptions.

Merchant strings compare poorly as character sequences: SequenceMatcher scores
"TST* KI'S RESTAURANT" against "KIS RESTAURANT LONDON" barely above noise, and
scores two unrelated strings that share a prefix far too high. Embeddings put
them in a space where the comparison means something.

Vectors are cached because they are deterministic for a given model and text —
so the same pair always scores the same, which matters when the score feeds a
dedup key or advances a schedule. Stored as JSON rather than a native vector
type so SQLite and PostgreSQL behave identically; the volume here is a couple
of thousand rows, not a similarity index.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DescriptionEmbedding(Base):
    __tablename__ = "description_embeddings"
    __table_args__ = (
        UniqueConstraint("model", "text_key", name="uq_description_embedding"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Which model produced it — changing models invalidates nothing, the new
    # model simply populates its own rows alongside.
    model: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # The normalised description, i.e. what was actually embedded.
    text_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[str] = mapped_column(Text, nullable=False)   # JSON array
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
