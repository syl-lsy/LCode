"""add session sandbox_id

Revision ID: a5fb527b21f8
Revises: 249f98e4efa3
Create Date: 2026-10-24

sessions 表新增 sandbox_id 列（沙箱健壮性改造包第 0 步）：记录会话当前绑定的
沙箱实例 ID，nullable=True 兼容历史行（未创建沙箱的会话为 NULL）。

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5fb527b21f8"
down_revision: str | Sequence[str] | None = "249f98e4efa3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sessions", sa.Column("sandbox_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sessions", "sandbox_id")
