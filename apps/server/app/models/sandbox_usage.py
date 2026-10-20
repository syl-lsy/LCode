"""sandbox_usage 表（契约 9/10）：沙箱计费分段（REQ-015 / AC-015-4）。

每个 running/paused 相位段一行，费用 = 时长 × 单价（运行 0.156 元/h、暂停保留
0.0021 元/GiB·h，费率读 SANDBOX_ 配置组）；ended_at 为空 = 该段仍在进行——
启动新相位前先封口上一段。started_at 是业务事实时间（非 DB 时钟），故无默认值。
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class SandboxUsage(Base):
    """沙箱计费分段（REQ-015/AC-015-4）：running/paused 每相位一段，未封口 = ended_at 空。"""

    __tablename__ = "sandbox_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    sandbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Numeric(asdecimal=True) 默认读回即 Decimal（F8：注解如实，防浮点口径误导）
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'CNY'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_sandbox_usage_session_started", "session_id", "started_at"),
        Index("ix_sandbox_usage_user_started", "user_id", "started_at"),
        enum_check("sandbox_usage", "provider", ["bailian", "opensandbox"]),
        enum_check("sandbox_usage", "phase", ["running", "paused"]),
    )
