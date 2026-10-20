"""真库 checkpointer（AsyncPostgresSaver）的 lifespan 装配（e2e 车道；REQ-004 约束④）。

默认（开发/生产/integration）**不启用**：`LCODE_E2E_CHECKPOINTER` 未设或不等于
"postgres" 时本模块零副作用（不建连、不建表、不替换 saver）。启用时（当前仅 e2e
子进程，见 `.env.test.example` 注记与 test/e2e/conftest.py），lifespan 期：

1. 构建独立 `AsyncConnectionPool`（psycopg；异步连接绑定事件循环，必须随应用起停，
   dependencies.get_checkpointer docstring 的既有结论）；
2. `AsyncPostgresSaver(pool).setup()` 幂等建 checkpoint 表族（checkpoints /
   checkpoint_writes / checkpoint_blobs）——断点恢复语义必须真库；
3. 经 `dependencies.set_checkpointer` 注入进程级单例，run_service 装配即时生效。

关闭期 `pool.close()`（teardown），随后 main.py 才释放业务连接池——顺序无强约束，
两者都在 lifespan 的事件循环内完成。连接串复用 `core/db.build_database_url()`（与
应用/迁移同口径；密钥纪律同 NFR-004：只读环境变量，串不落日志）。
"""

from __future__ import annotations

import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.core.db import build_database_url
from app.dependencies import set_checkpointer

E2E_CHECKPOINTER_ENV = "LCODE_E2E_CHECKPOINTER"
"""e2e 开关变量名（仅测试车道读取；不进 wire 契约、不进生产 .env.example）。"""

_POSTGRES_MODE = "postgres"
"""启用真库 checkpointer 的开关值（当前唯一非默认模式）。"""

_POOL_MIN = 1
_POOL_MAX = 5
"""e2e 体量的池界：远小于业务池（连接只被 checkpointer 序列化读写消费）。"""


async def setup_postgres_checkpointer_if_requested() -> AsyncPostgresSaver | None:
    """按环境开关装配真库 checkpointer；未启用返回 None（生产默认路径，零副作用）。"""
    if os.environ.get(E2E_CHECKPOINTER_ENV) != _POSTGRES_MODE:
        return None

    # build_database_url 是 asyncpg 方案（postgresql+asyncpg://）——psycopg 连接串
    # 必须去掉驱动段（postgresql://），同一套 DB_ 配置、两种驱动各自的 URL 形态。
    conninfo = (
        build_database_url().set(drivername="postgresql").render_as_string(hide_password=False)
    )
    pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=_POOL_MIN,
        max_size=_POOL_MAX,
        open=False,  # 事件循环内显式打开（psycopg 池的推荐形态）
        kwargs={"autocommit": True},  # setup() 的 CREATE INDEX CONCURRENTLY 不能在事务块内
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]  # psycopg 池的默认泛型参数与 langgraph 标注不一致（运行期同为 AsyncConnection）
    await saver.setup()  # 幂等建 checkpoint 表族（REQ-004：断点恢复语义真库）
    set_checkpointer(saver)
    return saver


async def teardown_postgres_checkpointer(saver: AsyncPostgresSaver | None) -> None:
    """关闭期释放 checkpointer 连接池（未启用时 saver 为 None，直接返回）。"""
    if saver is None:
        return
    await saver.conn.close()


__all__ = [
    "E2E_CHECKPOINTER_ENV",
    "setup_postgres_checkpointer_if_requested",
    "teardown_postgres_checkpointer",
]
