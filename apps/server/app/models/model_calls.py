"""model_calls 表（契约 8/10）：模型调用成本（REQ-015 / NFR-003）。

每次模型调用 100% 落一行（含失败行 status=error + error_type）；
思考 token 计入 output_tokens 口径（PRD 硬约束 9）；
cost 精度 numeric(12,6)（contracts.rest 的落库精度口径），币种单列（P0 恒 CNY）。
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._checks import enum_check
from app.models.base import Base


class ModelCall(Base):
    """模型调用成本（REQ-015）：100% 落库；思考 token 计入 output_tokens（同 Usage 口径）。"""

    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runs.id"), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Numeric(asdecimal=True) 默认读回即 Decimal（F8：注解如实，防浮点口径误导）
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'CNY'"))
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_model_calls_session_created", "session_id", "created_at"),
        Index("ix_model_calls_user_created", "user_id", "created_at"),
        Index("ix_model_calls_model", "model"),
        enum_check("model_calls", "status", ["success", "error"]),
    )
