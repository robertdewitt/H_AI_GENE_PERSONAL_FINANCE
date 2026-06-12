"""Per-user API token for non-interactive callers (LLM agents).

Tokens are 32 random bytes, base64url-encoded, shown to the user exactly
once at creation. Only the SHA-256 hash is persisted — a database leak
cannot be replayed as a live token. revoked_at != NULL means the token
is dead; we keep the row for audit instead of hard-deleting.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
