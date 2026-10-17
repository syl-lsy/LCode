"""Alembic 迁移环境（async 模板）：连接串从 `DB_` 配置组构造，ini 里不写任何凭证。

要点（A3 §7.1 / §7.2）：

- URL 复用 `app.core.db.build_database_url()`——与应用运行时同一口径（含转义规则），
  避免「迁移连 A 库、应用连 B 库」这类两处拼串的经典事故；
- `target_metadata = Base.metadata`：M2 任务 1 落地十表后 `--autogenerate` 才有 diff 依据
  （当前 metadata 为空，autogenerate 会产出空迁移——骨架期不生成业务表迁移）；
- checkpointer 三表（LangGraph `AsyncPostgresSaver.setup()`）**不在此迁移**：SDK 自管
  schema，部署期由 Makefile 目标执行一次（A3 §7.2）；
- 运行方式（从仓库根跑，基座铁律 6）：
  `uv run --package lcode-server alembic -c apps/server/alembic.ini upgrade head`
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.db import build_database_url
from app.models import Base

# Alembic Config 对象：提供当前 ini 文件里的配置项
config = context.config

# 日志配置来自 alembic.ini（[loggers] 等段）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的元数据来源：ORM 基类聚合的 metadata（M2 起逐表填充）
target_metadata = Base.metadata


def _database_url() -> str:
    """构造连库 URL，并把 `%` 转义为 `%%`。

    原因：URL 注入 configparser 后，单个 `%` 会被当作插值占位符（密码里带 `%` 时
    会报 InterpolationSyntaxError），必须按 configparser 语法转义。
    """
    return build_database_url().render_as_string(hide_password=False).replace("%", "%%")


def run_migrations_offline() -> None:
    """离线模式：只渲染 SQL 不连库（URL 取自配置组而非 ini）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：async engine（asyncpg）；NullPool 避免迁移期连接池残留。"""
    config.set_main_option("sqlalchemy.url", _database_url())

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
