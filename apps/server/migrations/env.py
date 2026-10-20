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

# 日志配置来自 alembic.ini（[loggers] 等段）。
# `disable_existing_loggers=False` 不可省：进程内调用 alembic（如 integration 层
# conftest 在 pytest_sessionstart 里 `command.upgrade`）时，默认的 True 会**禁用所有
# 既有 logger**——应用 stdlib 日志（异常处理器兜底路径）与本会话后续用例的 caplog
# 捕获会被静默掐断，且症状只在「unit+integration 同进程跑」时出现（难查）。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# autogenerate 的元数据来源：ORM 基类聚合的 metadata（M2 起逐表填充）
target_metadata = Base.metadata


def _database_url(*, for_configparser: bool) -> str:
    """构造连库 URL；两种调用口径的 `%` 处理不同（别合并成一个函数体）。

    - `for_configparser=True`（在线模式）：URL 经 `config.set_main_option` 注入 ini，
      configparser 会把单个 `%` 当插值占位符（密码含 `%` 时抛
      `InterpolationSyntaxError`）→ 必须按 configparser 语法转义为 `%%`；
    - `for_configparser=False`（离线 `--sql` 模式）：URL 直接交给方言渲染，
      **不经过 configparser** → 必须保持原样，否则 `%%` 会被当作 URL 字面量
      （`render_as_string` 的结果里密码会多出字符，渲染出错误的连接串）。
    """
    url = build_database_url().render_as_string(hide_password=False)
    return url.replace("%", "%%") if for_configparser else url


def run_migrations_offline() -> None:
    """离线模式：只渲染 SQL 不连库（URL 取自配置组而非 ini）。"""
    context.configure(
        url=_database_url(for_configparser=False),
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
    # 注入 ini 的 URL 必须走 configparser 转义（详见 _database_url docstring）
    config.set_main_option("sqlalchemy.url", _database_url(for_configparser=True))

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
