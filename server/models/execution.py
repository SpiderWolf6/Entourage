"""Execution models — tracks sprint runs and demo lifecycle."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, DateTime, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from server.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SprintRun(Base):
    """Records each sprint execution and PL review gate outcome."""
    __tablename__ = "sprint_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), index=True
    )
    sprint_number: Mapped[int] = mapped_column(Integer, default=0)
    # Status: "running" | "done" | "partial" | "failed" | "approved" | "rejected"
    status: Mapped[str] = mapped_column(String(20), default="running")
    files_written: Mapped[str] = mapped_column(Text, default="[]")   # JSON list
    review_summary: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")             # PL review notes / rejection reason
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
