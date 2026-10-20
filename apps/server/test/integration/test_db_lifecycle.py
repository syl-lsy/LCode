"""integration 层：DB 生命周期与十表落库契约（T1 锚点；AC-004-1 的库侧证据）。

- session 级（conftest）：重建 lcode_test → `alembic upgrade head`——本文件断言迁移产物；
- 测试级：每测前置 `TRUNCATE ... RESTART IDENTITY CASCADE`——后两个用例一组证明
  「前一用例提交的行不跨用例可见」（fixture 真在清，不是摆设）；
- 全文件 requires_db：库不可达 / DB_NAME 非 _test / 未配置时由 conftest 显式 skip
  （reason 汇总见 pytest -ra 输出），不静默消失、不让命令红掉。
"""

import pytest
from sqlalchemy import func, select, text

from app.models import Session

pytestmark = pytest.mark.requires_db

BUSINESS_TABLES = {
    "approvals",
    "diff_reviews",
    "events",
    "git_baselines",
    "messages",
    "model_calls",
    "runs",
    "sandbox_usage",
    "sessions",
    "tool_calls",
}


async def test_migration_created_all_ten_tables(db_session) -> None:
    """迁移产物 = 十张业务表（+ alembic_version）；checkpointer 三表不由业务迁移创建。"""
    result = await db_session.execute(
        text("select tablename from pg_tables where schemaname = 'public'")
    )
    names = {row[0] for row in result}
    assert names == BUSINESS_TABLES | {"alembic_version"}
    assert not names & {"checkpoints", "checkpoint_writes", "checkpoint_blobs"}


async def test_server_defaults_fill_row(db_session) -> None:
    """AC-004-1 库侧证据：服务端默认值（uuid/user_id/时间戳/枚举默认）真的在 DB 侧生效。"""
    row = Session(model="qwen3.7-plus", reasoning_effort="medium", sandbox_provider="bailian")
    db_session.add(row)
    await db_session.flush()

    assert row.id is not None  # gen_random_uuid()
    assert row.user_id == "local"  # NFR-008
    assert row.title == "新会话"
    assert row.status == "active"
    assert row.approval_mode == "manual"
    assert row.created_at is not None  # now()
    await db_session.commit()


async def test_per_test_truncate_row_visible_within_test(db_session) -> None:
    """基线：本测写入并提交的行在本测内可见（写入链路通）。"""
    db_session.add(Session(model="m", reasoning_effort="off", sandbox_provider="bailian"))
    await db_session.commit()

    count = await db_session.scalar(select(func.count()).select_from(Session))
    assert count == 1


async def test_per_test_truncate_clears_previous_test_rows(db_session) -> None:
    """前一用例提交的行不跨用例可见——每测 TRUNCATE ... RESTART IDENTITY CASCADE 的证据。"""
    count = await db_session.scalar(select(func.count()).select_from(Session))
    assert count == 0
