"""runs 表（契约 2/10）：一次用户消息触发的 agent 运行（REQ-001/003）。

status=waiting_approval 只记业务挂起态——interrupt 现场由 checkpointer 承载，
本表不反序列化 checkpoint（REQ-004 记忆双轨约束②）；异常收敛留 error_code/message。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class Run(Base):
    """agent 运行（REQ-001/003）：waiting_approval = 审批挂起中；stop/异常走终态三值。"""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_runs_session_started", "session_id", "started_at"),
        enum_check(
            "runs", "status", ["running", "waiting_approval", "completed", "stopped", "error"]
        ),
    )
