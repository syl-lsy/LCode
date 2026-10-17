"""FastAPI 依赖注入装配点（A3 §1.1）。

M1 骨架只落地请求级 DB 会话；其余 getter 依赖 M2 的实体（事件管线 / 沙箱管理器 /
模型注册表）才存在，为避免提前实现，这里先以注释登记挂点：

- `get_db`（本文件）——请求级事务边界：正常统一 commit、异常统一 rollback
  （基座铁律 10；service 内只 `flush()` 不 `commit()`）；
- `get_event_pipeline`（M2 任务 7）：`EventPipeline` 单例（进程内总线 + emitter）；
- `get_sandbox_manager`（M2 任务 4）：`SandboxManager` 单例（会话沙箱注册表）；
- `get_model_registry`（M2 任务 3）：`ModelRegistry` 单例（models.yaml 加载器）。

API 层只通过本模块取依赖；`services/` 由 API 用注入的会话构造（基座铁律 3/10）。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话：请求成功统一 commit、异常统一 rollback、退出即关闭。

    事务边界收在这一处（基座铁律 10），因此 service 层只 `flush()`——
    保证「一个请求 = 一个事务」的语义，异常时业务行与事件行一起回滚。
    需要先做非库耗时工作（外部 SDK / 沙箱调用）时，service 内部按短事务模式
    显式 `commit()` + `close()` 释放连接（见 references/service-layer.md §4.2）。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
