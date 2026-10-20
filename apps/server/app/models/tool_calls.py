"""tool_calls 表（契约 4/10）：工具调用（REQ-003/008）。

tool_call_id 为 wire 层调用标识（LangGraph/OpenAI 口径）——事件回放按它关联
tool_call_* 事件与历史卡片（AC-008-3）；失败调用如实落 status=error + error_*（AC-004-4），
result_preview 只存预览，truncated 标记是否被截断（原文在沙箱/事件里）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class ToolCall(Base):
    """工具调用（REQ-003/008）：tool_call_id 为 wire 层标识；error 行必须保真保留。"""

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_tool_calls_session_created", "session_id", "created_at"),
        Index("ix_tool_calls_run", "run_id"),
        Index("ix_tool_calls_call_id", "tool_call_id"),
        enum_check("tool_calls", "status", ["running", "success", "error"]),
    )
