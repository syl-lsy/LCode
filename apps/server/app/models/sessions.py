"""sessions 表（契约 1/10）：会话元数据（REQ-004/014）。

列序/类型/可空/默认/值域的唯一事实源：`contracts/tables.py` 的 `sessions` TableSpec；
本文件与 `test/unit/test_models_conformance.py` 一起构成「模型 ↔ 契约」漂移的 CI 拦截点。

model / reasoning_effort / sandbox_provider 为 NOT NULL 且无默认：新建会话时由
session_service 按模型注册表与配置显式给定（不设错误的兜底默认值）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class Session(Base):
    """会话（REQ-004/014）：标题/模型/档位/审批模式/沙箱 provider 均为会话级可变更项。"""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'新会话'"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    model: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String, nullable=False)
    approval_mode: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'manual'")
    )
    sandbox_provider: Mapped[str] = mapped_column(String, nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "last_active_at"),
        enum_check("sessions", "status", ["active", "archived"]),
        enum_check("sessions", "reasoning_effort", ["off", "low", "medium", "high"]),
        enum_check("sessions", "approval_mode", ["manual", "auto"]),
        enum_check("sessions", "sandbox_provider", ["bailian", "opensandbox"]),
    )
