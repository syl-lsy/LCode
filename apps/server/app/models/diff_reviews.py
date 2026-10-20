"""diff_reviews 表（契约 6/10）：diff 审查轮次（REQ-007）。

round 与基线链一一对应；(session_id, round) 唯一（uq_diff_reviews_session_round）。
files 存内部超集（A3 §7.1）：[{path, old_path, change, additions, deletions, binary,
patch?, blob_ref?}]——wire 层投影为 DiffFileSummary（事件）/ DiffFileDetail（REST）；
patch 在 diff_ready 时刻固化（沙箱可能随后 pause/kill，按需重算不可靠）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class DiffReview(Base):
    """diff 审查轮次（REQ-007）：(session_id, round) 唯一；files 为内部超集 jsonb。"""

    __tablename__ = "diff_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    baseline_kind: Mapped[str] = mapped_column(String, nullable=False)
    baseline_ref: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_commit: Mapped[str] = mapped_column(String, nullable=False)
    files: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    additions_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    deletions_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_diff_reviews_session_round", "session_id", "round", unique=True),
        enum_check("diff_reviews", "verdict", ["pending", "accepted", "rejected"]),
        enum_check("diff_reviews", "baseline_kind", ["head", "init", "accepted"]),
    )
