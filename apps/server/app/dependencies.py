"""FastAPI 依赖注入装配点（A3 §1.1）。

进程级单例（API 层经本模块取用，路由文件不见实现类型）：

- `get_db`（本文件）——请求级事务边界：正常统一 commit、异常统一 rollback
  （基座铁律 10；service 内只 `flush()` 不 `commit()`）；
- `get_event_pipeline`（M2 任务 7）：`EventPipeline` 单例（进程内总线 + emitter）；
- `get_sandbox_manager`（M2 任务 4）：`SandboxManager` 单例（会话沙箱注册表）；
- `get_model_registry`（M2 任务 3，已落地）：`ModelRegistry` 单例（models.yaml 加载器）；
- `get_checkpointer` / `get_model_factory`（M2 任务 7）：run 执行单元的两个接缝
  （断点恢复 saver 与按角色取模型的工厂），e2e/测试经 `app.dependency_overrides` 注入替身；
- `get_run_service`（M2 任务 7/8）：run 编排 service 装配（消息/停止/成本/沙箱/删除共用），
  其中**在跑 run 句柄表必须是进程级单例**（run 由 POST /messages 的请求实例启动，stop/
  DELETE 是另外的请求——句柄表挂 service 实例上 stop 永远找不到执行中的任务）。

API 层只通过本模块取依赖；`services/` 由 API 用注入的会话构造（基座铁律 3/10）。
本文件是 `api/` ↔ `agent/` 之间的合法桥（A3 §1.2 禁止表只拦 `api/` → `agent/`
直接引用）——注册表的类型锚点收在这里，路由文件只见 `ModelRegistryDep` / `RunServiceDep`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Annotated, Any

from fastapi import Depends
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.models_registry import ModelRegistry, model_registry
from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events import EventPipeline
from app.sandbox.base import SandboxOutputChunk
from app.sandbox.manager import SandboxLifecycleEvent, SandboxManager, SandboxUsageRecord
from app.services.cost_service import record_sandbox_usage_short_tx
from app.utils import clock
from lcode_contracts.events import SandboxOutputEvent

ModelFactoryLike = Any
"""`Callable[[str], BaseChatModel]` 的宽松别名（本模块不 import `app.agent.assembly`，
工厂的真实类型锚点在 run_service；默认 None = 走 `get_llm` 真工厂）。"""


def get_model_registry() -> ModelRegistry:
    """ModelRegistry 单例：models.yaml 在模块导入期已加载并 fail-fast 校验（A3 §2.3）。"""
    return model_registry


ModelRegistryDep = Annotated[ModelRegistry, Depends(get_model_registry)]
"""路由层依赖锚点：`api/` 由此拿注册表，不直接 import `app.agent`。"""


_event_pipeline = EventPipeline()
"""进程级单例：全系统唯一事件出口（校验→seq 落库→广播）+ 其进程内总线。

emitter 落库用 `core/db` 的会话工厂自取短会话（A3 §7.3：每事件一个独立短事务），
不依赖请求级会话——REST 侧与 run 后台任务共用同一出口，重放与实时同源。
"""


def get_event_pipeline() -> EventPipeline:
    """EventPipeline 单例（REST service 与 SSE 端点共用的总线/出口）。"""
    return _event_pipeline


EventPipelineDep = Annotated[EventPipeline, Depends(get_event_pipeline)]
"""SSE 端点依赖锚点：`api/` 由此取总线订阅与出口，不直接构造 emitter。"""


# ---------------------------------------------- 沙箱事件桥（A3 §6.2：manager 直投 emitter）

_background_tasks: set[asyncio.Task[None]] = set()
"""桥接发射的后台任务句柄表（防 GC；done 回调自清并检索异常，消除 unretrieved 警告）。"""

_main_loop: asyncio.AbstractEventLoop | None = None
"""服务主事件循环（lifespan 启动期经 `set_main_loop` 捕获；线程上下文发射的投递目标）。"""


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """捕获服务主循环（**仅 lifespan 启动期调用一次**）。

    工具执行跑在 worker 线程（anyio to_thread：contextvars 会携带、但线程内没有
    running loop）——沙箱输出回调从线程上下文发射事件时，跨线程投递需要主循环引用。
    """
    global _main_loop
    _main_loop = loop


def _log_launch_failure(exc: BaseException | None) -> None:
    if exc is not None:
        BusinessLog(
            message="沙箱事件桥接发射失败（已忽略，不阻断沙箱生命周期）",
            action="发射沙箱事件",
            entity="event",
        ).warning(exc if isinstance(exc, Exception) else None)


def _launch(coro: Coroutine[Any, Any, Any]) -> None:
    """把同步回调侧的异步发射挂到服务主事件循环。

    两个调用上下文（实测踩坑，e2e 演示链暴露）：

    - **异步上下文**（manager.create 等 run_service 编排路径）：直接 `create_task`；
    - **工具执行线程**（deepagents 把同步 execute 派到 worker 线程——anyio 会携带
      contextvars 但线程内 `get_running_loop()` 抛 RuntimeError，stdout 块曾被静默
      丢弃）：经 `set_main_loop` 捕获的主循环 `run_coroutine_threadsafe` 跨线程投递。

    无主循环（导入期 / 纯同步测试上下文）= 无消费者，安静丢弃。事件间相对顺序不构成
    契约（seq 只保序不保密度），跨线程投递的次序漂移可接受。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        main_loop = _main_loop
        if main_loop is None or main_loop.is_closed():
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        future.add_done_callback(lambda fut: _log_launch_failure(fut.exception()))
        return
    task = loop.create_task(coro)
    _background_tasks.add(task)

    def _settle(done: asyncio.Task[None]) -> None:
        _background_tasks.discard(done)
        _log_launch_failure(done.exception())  # 检索即消除 "exception was never retrieved" 警告

    task.add_done_callback(_settle)


def sandbox_lifecycle_payload(event: SandboxLifecycleEvent) -> SandboxOutputEvent | None:
    """相位迁移 → `sandbox_output(lifecycle)` 载荷；无可引用身份的迁移返回 None。

    契约 `SandboxOutputEvent.sandbox_id` 为必填 str——creating 相位（实例尚未
    provision）与 create 失败的 crashed 迁移没有 id 可引用，跳过不造占位值
    （诚实口径：终端不渲染无身份的生命周期行）；`none` 是账本初值，从不迁移产生。
    """
    if event.sandbox_id is None or event.phase == "none":
        return None
    return SandboxOutputEvent(
        seq=0,  # 真实 seq 由 EventPipeline 统一分配
        session_id=event.session_id,
        run_id=None,
        agent=None,
        created_at=event.at,
        sandbox_id=event.sandbox_id,
        channel="lifecycle",
        phase=event.phase,  # 迁移面无 none（上分支已排除）；契约 phase 字面量为其子集
        reason=event.reason,
        provider=event.provider,
    )


def sandbox_output_payload(
    session_id: str, sandbox_id: str, chunk: SandboxOutputChunk
) -> SandboxOutputEvent | None:
    """命令输出块 → `sandbox_output(stdout|stderr)` 载荷；无 exec_id 的块返回 None。

    契约：stdout/stderr 必带 exec_id——tap 上下文之外的命令（基线助手、重启探测等
    无工具路径）没有可标注的 tool_call_id，跳过；工具路径的注解由 tap 中间件经
    `app/utils/exec_context.py` 接力（见其模块 docstring）。
    """
    if chunk.exec_id is None:
        return None
    return SandboxOutputEvent(
        seq=0,  # 真实 seq 由 EventPipeline 统一分配
        session_id=session_id,
        run_id=None,
        agent=None,
        created_at=clock.utc_now(),
        sandbox_id=sandbox_id,
        channel=chunk.stream,
        data=chunk.data,
        exec_id=chunk.exec_id,
    )


def _default_sandbox_lookup(session_id: str) -> Any:
    """session_id → 存活沙箱实例（进程单例 manager；桥接闭包的缺省查找面）。"""
    return get_sandbox_manager().get(session_id)


def build_sandbox_event_bridges(
    pipeline: EventPipeline, *, lookup: Any = None
) -> tuple[Any, Any, Any]:
    """构造 manager 三相回调 → 事件出口/计费落库的桥（`LifecycleSink/OutputSink/UsageSink`）。

    依赖方向（A3 §1.2）：`sandbox/` 禁止 import `events/`/`models/`，桥只能住在装配层
    （本模块）——manager 只持有无业务语义的回调面。`lookup` 是 session_id → 存活实例的
    查找面（缺省 = 进程单例 manager；测试可注入替身 manager）。usage 封口段走
    `record_sandbox_usage_short_tx`（短事务，失败只记日志）。
    """

    def on_lifecycle(event: SandboxLifecycleEvent) -> None:
        payload = sandbox_lifecycle_payload(event)
        if payload is not None:
            _launch(pipeline.emit(payload))

    def on_output(session_id: str, chunk: SandboxOutputChunk) -> None:
        finder = lookup if lookup is not None else _default_sandbox_lookup
        client = finder(session_id)
        if client is None:
            return
        payload = sandbox_output_payload(session_id, client.id, chunk)
        if payload is not None:
            _launch(pipeline.emit(payload))

    def on_usage(record: SandboxUsageRecord) -> None:
        _launch(record_sandbox_usage_short_tx(record))

    return on_lifecycle, on_output, on_usage


_sandbox_bridges = build_sandbox_event_bridges(_event_pipeline)
"""三相回调桥（lifecycle/output → 域事件；usage 封口段 → sandbox_usage 行）。

桥在单例 manager **构造前**建好、但内部查找与发射都是惰性的（回调触发时 `_sandbox_manager`
与库连接早已就位）——manager 持桥、桥查 manager 的环由"闭包延迟求值"解开。
"""

_sandbox_manager = SandboxManager(
    on_lifecycle=_sandbox_bridges[0],
    on_output=_sandbox_bridges[1],
    on_usage_segment=_sandbox_bridges[2],
)
"""进程级单例：会话沙箱注册表（相位机/空闲 pause/计费分段的持有者，账本跨请求存活）。

生命周期挂点（A3 §4.3）：启动装配（空闲计时循环）与关停 pause 策略由 main.py 的
lifespan 接线，本 getter 只提供实例，不启停；相位迁移/输出块/封口段经上面的桥直投
事件出口与 `sandbox_usage` 表（A3 §6.2 的 sandbox_output 事件源）。
"""


def get_sandbox_manager() -> SandboxManager:
    """SandboxManager 单例（会话沙箱注册表；provider 切换粒度 = 会话级）。"""
    return _sandbox_manager


def set_sandbox_manager(manager: SandboxManager) -> None:
    """替换进程级沙箱管理器（**仅 e2e/测试车道**；服务开放流量前调用，见 main.py lifespan）。

    桥接闭包（`_sandbox_bridges` 的 `_default_sandbox_lookup`）经 `get_sandbox_manager()`
    在回调触发时惰性取用——替换后事件桥与计费落库自动跟随新管理器，无需重建桥。
    """
    global _sandbox_manager
    _sandbox_manager = manager


SandboxManagerDep = Annotated[SandboxManager, Depends(get_sandbox_manager)]
"""沙箱管理器依赖锚点：`api/` 由此传给 service，不直接 import `app.sandbox`。"""


_checkpointer = InMemorySaver()
"""进程级 run 断点恢复 saver（LangGraph checkpointer 接缝）。

**M2 现状（如实注明）**：默认 `InMemorySaver`——进程内断点。审批 interrupt 的跨进程
恢复（REQ-004：`AsyncPostgresSaver`）需要 lifespan 期建连 + `setup()`（psycopg 异步
连接绑定事件循环，必须随应用生命周期起停），属部署/e2e 车道的挂点；e2e 与测试经
`app.dependency_overrides[get_checkpointer]` 注入替身或真 saver。
"""


def get_checkpointer() -> InMemorySaver:
    """run 断点恢复 saver 单例（M2 默认 InMemorySaver，见上方注记）。"""
    return _checkpointer


def set_checkpointer(saver: Any) -> None:
    """替换进程级 checkpointer（**仅 e2e/部署车道**；lifespan 期调用、流量前生效）。

    e2e 经 `app/core/postgres_checkpointer.py` 装配真 `AsyncPostgresSaver`（REQ-004
    约束④）——连接池随应用生命周期起停，替换点必须在服务开放流量之前。
    """
    global _checkpointer
    _checkpointer = saver


def get_model_factory() -> ModelFactoryLike:
    """按角色取模型的工厂接缝（默认 None = RunExecutor 走 `get_llm` 真工厂）。

    e2e/测试经 `app.dependency_overrides` 注入假模型工厂（剧本化回复）；生产路径保持
    None，工厂链与密钥读取全部走 `app.agent.llm_factory`（密钥只从环境变量取，NFR-004）。
    """
    return None


def get_db_factory() -> async_sessionmaker[AsyncSession]:
    """会话工厂依赖：给 SSE 等**长生命周期**请求做一次性短会话校验用。

    请求级 `get_db` 的会话随整条响应存活——流式端点若用它做流前校验，会把连接池连接
    钉在整条流上（多客户端并发时耗尽连接池）。需要短命会话的端点取本工厂自管开关。
    """
    return get_session_factory()


def _build_active_run_registry() -> Any:
    """在跑 run 句柄表的进程级单例（类型 = run_service.ActiveRunRegistry，局部导入防环）。"""
    from app.services.run_service import ActiveRunRegistry

    return ActiveRunRegistry()


_active_runs = _build_active_run_registry()
"""进程级在跑 run 句柄表（stop/DELETE 借此定位执行中的任务；跨请求存活，见 get_run_service）。"""


def _build_approval_resume_registry() -> Any:
    """审批 resume 上下文的进程级单例（类型 = run_service.ApprovalResumeRegistry）。"""
    from app.services.run_service import ApprovalResumeRegistry

    return ApprovalResumeRegistry()


_approval_resume = _build_approval_resume_registry()
"""进程级审批续跑上下文（waiting 段在 A 请求收口、决策 resume 在 B 请求——必须跨请求）。"""


def get_run_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    pipeline: Annotated[EventPipeline, Depends(get_event_pipeline)],
    manager: Annotated[SandboxManager, Depends(get_sandbox_manager)],
    registry: Annotated[ModelRegistry, Depends(get_model_registry)],
    checkpointer: Annotated[InMemorySaver, Depends(get_checkpointer)],
    model_factory: Annotated[ModelFactoryLike, Depends(get_model_factory)],
) -> Any:
    """run 编排 service 装配（消息/停止/成本/沙箱/删除/审批恢复共用的唯一装配点）。

    本函数是 `api/` → `services/` 的合法桥（A3 §1.2）：run_service 需要 agent 编排与
    沙箱管理器，路由文件不得直接 import 这两层——类型锚点收在这里。
    """
    from app.services.diff_service import prepare_review_short_tx  # 局部导入：防环
    from app.services.run_service import RunService  # 局部导入：避免 services↔dependencies 环

    async def _prepare_diff(launch: Any) -> None:
        """run 正常收口后的 diff 就绪（M3 Phase 4；自持短事务，git 在沙箱内执行）。"""
        await prepare_review_short_tx(
            session_id=launch.session_id,
            run_id=launch.run_id,
            user_id=launch.user_id,
            manager=manager,
            pipeline=pipeline,
        )

    return RunService(
        db,
        pipeline=pipeline,
        manager=manager,
        checkpointer=checkpointer,
        model_factory=model_factory,
        model_registry=registry,
        active_runs=_active_runs,
        approval_resume=_approval_resume,
        diff_runner=_prepare_diff,
    )


RunServiceDep = Annotated[Any, Depends(get_run_service)]
"""run 编排依赖锚点：消息（启动）/runs（停止）/costs+sandbox（沙箱投影）/sessions（删除）。"""


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话：请求成功统一 commit、异常统一 rollback、退出即关闭。

    事务边界收在这一处（基座铁律 10），因此 service 层只 `flush()`——
    保证「一个请求 = 一个事务」的语义，异常时业务行与事件行一起回滚。
    需要先做非库耗时工作（外部 SDK / 沙箱调用）时，service 内部按短事务模式
    显式 `commit()` + `close()` 释放连接（见 references/service-layer.md §4.2；
    `message_service.post_message` 是契约钉死的例外落点，A3 §7.3）。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "EventPipelineDep",
    "ModelFactoryLike",
    "ModelRegistryDep",
    "RunServiceDep",
    "SandboxManagerDep",
    "build_sandbox_event_bridges",
    "get_checkpointer",
    "get_db",
    "get_db_factory",
    "get_event_pipeline",
    "get_model_factory",
    "get_model_registry",
    "get_run_service",
    "get_sandbox_manager",
    "sandbox_lifecycle_payload",
    "sandbox_output_payload",
    "set_checkpointer",
    "set_main_loop",
    "set_sandbox_manager",
]
