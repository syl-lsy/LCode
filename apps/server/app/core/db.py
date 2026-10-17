"""数据库引擎与会话工厂：数据库访问的**唯一入口**（基座铁律 2；A3 §1.1 偏差 2）。

落点说明（ADR-004 偏差 2）：基座把引擎单例放在 `app/model/engine.py`，LCode 收敛到
`app/core/db.py`——LCode 有两类会话消费者（请求级 `dependencies.get_db` 与 agent
运行时自取短会话，A3 §7.3），引擎 + 工厂合一与配置/中间件同居 `core/`，并使
`migrations/env.py` 能复用同一个 URL 构造（避免连接串口径两处漂移）。

事务边界（A3 §7.3）：
- REST 请求 → `dependencies.get_db`（请求级：正常统一 commit、异常统一 rollback）；
- Agent 运行时（RunExecutor / EventPipeline / SandboxManager）→ 本模块的
  `get_session_factory()` 自取短会话，**每事件/每分段一个短事务立即 commit**。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import db_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_database_url() -> URL:
    """从 `DB_` 配置组拼异步连接串（asyncpg）。

    用 `URL.create` 而非手工 f-string：密码含 `@`/`/`/`:` 等字符时自动转义
    （drivers 侧不转义会解析出错误的 host/port）。`migrations/env.py` 复用本函数，
    保证 alembic 与应用指向同一实例、同一口径。
    """
    return URL.create(
        "postgresql+asyncpg",
        username=db_settings.user,
        password=db_settings.password,
        host=db_settings.host,
        port=db_settings.port,
        database=db_settings.name,
    )


def get_engine() -> AsyncEngine:
    """懒加载引擎单例：连接池参数全部集中在 `DB_` 配置组（基座铁律 2）。

    首次调用创建引擎对象（**不建立连接**），此后全进程复用同一连接池；
    `pool_pre_ping=True` 让空闲连接在借出前探活，避免 PG 侧超时断连导致的首请求失败。
    """
    global _engine

    if _engine is None:
        _engine = create_async_engine(
            build_database_url(),
            pool_size=db_settings.pool_size,
            max_overflow=db_settings.max_overflow,
            pool_pre_ping=True,
            echo=db_settings.echo,
        )

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """会话工厂单例：请求级与 agent 运行时共用（两类上下文的事务语义见模块 docstring）。"""
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,  # commit 后对象仍可读：service 返回 DTO 前不触发额外查询
        )

    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """短事务自管会话（agent 运行时用）：正常 commit、异常 rollback、退出即关闭。

    与请求级 `dependencies.get_db` 的差别只有「谁调用」——语义一致（先落库后广播
    要求落库是已提交事实），运行时绝不长事务占连接（A3 §7.3）。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """释放引擎连接池（应用 shutdown 用；测试收尾同样调用，避免连接泄漏）。"""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
