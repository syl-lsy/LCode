"""git_baselines 表（契约 7/10）：git 基线链（REQ-007 / 契约域 5）。

round=0 为起点（kind=head 或 init，非 git 输入按 AC-002-5 兜底 git init），
accepted 逐轮追加；(session_id, round) 唯一（uq_git_baselines_session_round），
round↔kind 不变量由 contracts.baseline 的契约域 5 校验承载，本表只做物理存储。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class GitBaseline(Base):
    """git 基线链（REQ-007/域 5）：round=0 起点 + accepted 追加；(session_id, round) 唯一。"""

    __tablename__ = "git_baselines"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    commit: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_git_baselines_session_round", "session_id", "round", unique=True),
        enum_check("git_baselines", "kind", ["head", "init", "accepted"]),
    )
