"""events 表（契约 10/10）：领域事件库（REQ-008 / AC-008-3）。

界面重建的唯一回放来源：(session_id, seq) 唯一承载顺序与 Last-Event-ID 续传
（uq_events_session_seq）；agent 为 AgentRef 快照（null = 主智能体，A3 §6）；
data 为事件除信封列外的载荷（扁平事件去掉信封列后整体入库）。
注意 type **不设** CHECK 值域：事件类型注册表在 contracts.events（含 P1 占位），
表侧留白保证新增事件类型是契约层动作、不是迁移动作（A3 §7.1 口径）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Event(Base):
    """领域事件库（REQ-008）：(session_id, seq) 唯一；界面重建的唯一回放来源。"""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    agent: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_events_session_seq", "session_id", "seq", unique=True),
        Index("ix_events_session_run", "session_id", "run_id"),
    )
