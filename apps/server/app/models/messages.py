"""messages 表（契约 3/10）：消息（REQ-001/004）。

role 只两值（user/assistant）：工具/系统消息不在本表——工具明细走 tool_calls，
系统提示与压缩提示走 events（AC-008-2 不透传原始事件）。agent_id 非空 = 子智能体消息；
structured 承载 REQ-011 结构化输出载荷（todo/findings 等，按事件类型定形）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class Message(Base):
    """消息（REQ-001/004）：agent_id 非空 = 子智能体消息；structured 为 REQ-011 载荷。"""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    structured: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        Index("ix_messages_session_run", "session_id", "run_id"),
        enum_check("messages", "role", ["user", "assistant"]),
        enum_check(
            "messages",
            "finish_reason",
            ["stop", "length", "content_filter", "error", "cancelled"],
            nullable=True,
        ),
    )
