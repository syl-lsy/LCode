"""真库 checkpointer（AsyncPostgresSaver）的 lifespan 装配（REQ-004 约束④，m3-spikes §2）。

开关口径（M3 D10）：

- `AGENT_CHECKPOINTER=postgres`（默认）：启用；`memory`：零副作用（不建连、不建表、
  不替换 saver，进程内 `InMemorySaver` 生效）——测试车道与无 PG 的轻量部署用；
- `LCODE_E2E_CHECKPOINTER`（仅测试车道读取，不进生产模板）是**强制口**：设了就覆盖
  配置值（e2e 子进程固定 `postgres`，见 `.env.test.example` 注记与 test/e2e/conftest.py）。
  **`ENVIRONMENT=production` 时该变量一律被忽略**（安全审计 P3-2）：一个测试专用变量
  不得在生产把真库 saver 静默降级为 InMemorySaver（进程重启后审批断点/resume 播种全丢）。

启用时，lifespan 期（连接池绑定创建时的事件循环，必须随应用起停）：

1. 构建独立 `AsyncConnectionPool`（psycopg；异步连接绑定事件循环，见
   dependencies.get_checkpointer docstring 的既有结论）；
2. `AsyncPostgresSaver(pool).setup()` 幂等建 checkpoint 表族（checkpoints /
   checkpoint_writes / checkpoint_blobs）——断点恢复语义必须真库；
3. 经 `dependencies.set_checkpointer` 注入进程级单例，run_service 装配即时生效。

连接串：`core/db.build_database_url()` 是 asyncpg 方案（`postgresql+asyncpg://`）——
psycopg 连接串必须 `.set(drivername="postgresql")` 去掉驱动段；池须 `autocommit=True`
（否则全新库上 `setup()` 的 `CREATE INDEX CONCURRENTLY` 报 ActiveSqlTransaction）。
密钥纪律同 NFR-004：只读环境变量，串不落日志。

关闭期 `pool.close()`（teardown），随后 main.py 才释放业务连接池——两者都在 lifespan
的事件循环内完成。
"""

from __future__ import annotations

import os

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.core.config import agent_settings, common_settings
from app.core.db import build_database_url
from app.core.logger import BusinessLog
from app.dependencies import set_checkpointer

E2E_CHECKPOINTER_ENV = "LCODE_E2E_CHECKPOINTER"
"""e2e 强制口变量名（仅测试车道读取；不进 wire 契约、不进生产 .env.example）。"""

_POSTGRES_MODE = "postgres"
_MEMORY_MODE = "memory"

_CHECKPOINTER_MODES: frozenset[str] = frozenset({_POSTGRES_MODE, _MEMORY_MODE})

_POOL_MIN = 1
_POOL_MAX = 5
"""e2e 体量的池界：远小于业务池（连接只被 checkpointer 序列化读写消费）。"""


def _resolve_mode() -> str:
    """解析生效的 saver 后端：e2e 强制口 > AGENT_CHECKPOINTER 配置（非法值 fail loudly）。

    `ENVIRONMENT=production` 时**忽略** `LCODE_E2E_CHECKPOINTER`（安全审计 P3-2）：它是
    测试车道的强制口，若在生产生效，一个环境变量就能把真库 saver 静默降级为
    InMemorySaver——进程重启后审批断点/resume 播种全丢。忽略时记 WARNING 留痕。
    """
    override = os.environ.get(E2E_CHECKPOINTER_ENV)
    if override and common_settings.environment.strip().lower() == "production":
        BusinessLog(
            message=f"{E2E_CHECKPOINTER_ENV} 在 production 下被忽略（沿用 AGENT_CHECKPOINTER）",
            action="装配断点存储",
            entity="config",
        ).warning()
        override = None
    mode = override if override is not None and override != "" else agent_settings.checkpointer
    if mode not in _CHECKPOINTER_MODES:
        raise ValueError(
            f"checkpointer 后端非法：{mode!r}（只允许 postgres / memory；"
            f"见 AGENT_CHECKPOINTER / {E2E_CHECKPOINTER_ENV}）"
        )
    return mode


async def setup_postgres_checkpointer_if_requested() -> AsyncPostgresSaver | None:
    """按配置装配真库 checkpointer；`memory` 模式返回 None（零副作用）。"""
    if _resolve_mode() != _POSTGRES_MODE:
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
