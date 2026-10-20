"""approvals 表（契约 5/10）：审批记录（REQ-006/013）。

决策人/时间/决策全量留痕（AC-006-2）：pending 为挂起态（AC-006-5，挂起可跨刷新，
无过期字段）；AUTO 模式（REQ-013）下落 status=auto_approved、decided_by='classifier'、
reason=分类器理由——AC-013-3 与 PRD 风险表「AUTO 决策全量留痕可审计」的唯一审计面。
人工四决策（approve/edit/reject/respond）经决策端点落行；「持续批准」由 scope=session
表达（P0 仅会话级，规则持久化 API 归 P1）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class Approval(Base):
    """审批记录（REQ-006/013）：决策留痕；AUTO 决策同表同列（audit 单一事实源）。"""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_level: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    scope: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'once'"))
    edited_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_approvals_session_status", "session_id", "status"),
        Index("ix_approvals_user_status", "user_id", "status"),
        enum_check("approvals", "risk_level", ["medium", "high"]),
        enum_check(
            "approvals",
            "status",
            ["pending", "approved", "rejected", "edited", "responded", "auto_approved"],
        ),
        enum_check(
            "approvals", "decision", ["approve", "edit", "reject", "respond"], nullable=True
        ),
        enum_check("approvals", "scope", ["once", "session"]),
    )
