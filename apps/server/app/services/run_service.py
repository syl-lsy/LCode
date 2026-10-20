"""run 编排域服务（A3 §5.5/§6.1/§7.3）：API 通往 Agent 的**唯一入口**。

职责（本车道 = 流式接口车道的运行编排半边）：

- **start_run**：把 `RunLaunch` 装配成后台任务——`_RunEventSink` 把 RunExecutor 吐出的
  原始 astream 三元组经 `EventTranslator` 翻成契约事件、经 `EventPipeline` 落库广播
  （先落库后广播）；assistant 消息沿途落库（AC-001-2「已生成内容保留」与 AC-004-1
  历史持久化的载体）。ack 不等 run——`asyncio.create_task` 即返回。
- **stop_run**：协作式停止 + **诚实收敛**——`request_stop` 取消执行任务，`await` 到
  收口完成才返回 ack：`message_finished(cancelled)`（在飞消息补收口）+ `run_status(stopped)`
  + run 行终态都已落库。幂等：对已 stopped 的 run 重复 stop 恒 200 + stopped（契约
  `StopRunAck` 注释）；其余终态（completed/error）→ `RUN_NOT_ACTIVE`（409，契约 errors）。
- **delete_session**：AC-002-3——先停活动 run、再 kill 沙箱（manager.release，kill 失败
  如实抛错、会话不删可重试），最后按「子表 → runs → sessions」清业务行（外键无级联
  删除；清行走 ORM 对象删除，SQLAlchemy 对每行生成参数化 DELETE，不拼接 SQL 文本）。
- **sandbox_status / sandbox_estimate**：查询面投影（`stable_phase` 稳态口径）+ cost_service
  的分段估算（本模块是沙箱管理器的持有者，事件侧相位折叠在此发生）。

事务边界（A3 §7.3）：本服务有两类上下文——请求级 `db`（stop/查询/删除：只 flush，提交在
get_db）；后台任务与收口（事件 emit、model_calls、assistant 消息行、run 行终态：自取
`core/db` 短会话立即 commit——run 不是请求，绝不长事务占连接）。

**一轮对话只产生一条 assistant 收口（agent 归属统一）**：translator 对内层 execute
子图与主图状态回声按上游 id 去重、合并 tap 附言，并在 finish_run 冲刷在飞工具收口。

run_status(started) 的来源：entry 节点经 custom 流发出（`agent/nodes/entry.py`），translator
透传——本服务**不**再补发一次（会双发）；translator.start_run() 保留给无 tap 的裸路径。

依赖方向（A3 §1.2）：`services → agent/runtime` 与 `services → sandbox/manager` 的例外
落点就是本文件（任务 7/8 的编排车道）；模型注册表经 TYPE_CHECKING 引用（同 session_service）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.models_registry import SessionModelCfg
from app.agent.runtime import RunExecutor, RunOutcome
from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events import EventPipeline, EventTranslator, ModelCallRecord
from app.exception.run import RunNotActive, RunNotFound
from app.exception.session import SessionNotFound
from app.models import Event as EventRow
from app.models import Message, ModelCall, Run, SandboxUsage, Session, ToolCall
from app.sandbox.manager import SandboxManager
from app.services.base import BaseService
from app.services.cost_service import (
    ModelCallEntry,
    estimate_sandbox_cost,
    model_cost_of,
    record_model_call_short_tx,
)
from app.services.message_service import RunLaunch
from app.utils.error_classifier import classify_run_error
from lcode_contracts.common import AgentRef, Usage
from lcode_contracts.events import (
    BaseEvent,
    MessageDeltaEvent,
    MessageFinishedEvent,
    RunStatusEvent,
)
from lcode_contracts.rest import SandboxCostEstimate, SandboxStatus, StopRunAck

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from app.agent.assembly import ModelFactory
    from app.agent.models_registry import ModelRegistry
    from lcode_contracts.common import EffortLevel

ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "waiting_approval")
"""活动 run 状态（与会话/消息域服务同口径；stop 的 409/幂等判定依据）。"""

_OUTCOME_TO_RUN_STATUS: dict[str, str] = {
    "finished": "completed",
    "stopped": "stopped",
    "error": "error",
}
"""RunExecutor 终态 → runs.status（契约五值；finished 折叠为 completed）。"""

_MODEL_CALL_ROLE_MAP: dict[str, str] = {
    "main": "main",
    "general-purpose": "main",  # 通用子智能体继承主智能体模型角色（subagents.py 表）
    "explorer": "explorer",
    "coder": "coder",
    "reviewer": "reviewer",
}
"""translator 记账行的 role 原值 → DB role（未知与 None 一律回落 main，role 永不为 NULL）。"""


@dataclass(frozen=True, slots=True)
class RunView:
    """run 状态查询投影（service 内部/测试面；契约无独立 run 详情端点）。"""

    run_id: str
    session_id: str
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


class RunHandle:
    """一条在跑 run 的进程内句柄（stop 的操作面；run 结束即摘除）。"""

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        executor: RunExecutor,
        task: asyncio.Task[None],
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.executor = executor
        self.task = task


class ActiveRunRegistry:
    """进程级在跑 run 句柄表（**跨请求存活**）。

    run 由 POST /messages 的请求实例启动，stop/DELETE 是**另外的请求**——句柄表若挂在
    service 实例上，stop 永远找不到执行中的任务（实测踩坑：stop 走「无句柄」分支，
    只落 run 行终态、补不回 `message_finished(cancelled)`）。因此本表必须是进程单例
    （dependencies 注入）；P0 单进程，无需跨进程分发。
    """

    def __init__(self) -> None:
        self._handles: dict[str, RunHandle] = {}

    def register(self, handle: RunHandle) -> None:
        self._handles[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    def get_by_session(self, session_id: str) -> RunHandle | None:
        """按**会话 id** 定位在跑句柄（DELETE 会话专用）。

        键口径：主表 `_handles` 恒以 **run_id** 为键（stop_run/注册/pop 都依赖该口径），
        会话 id 不是键——DELETE 拿到的是会话 id，只能按句柄值匹配。同步遍历期间没有
        await（单事件循环不会与 register/pop 交错），因此不存在第二索引「注册了但没
        来得及同步」的中间态；P0 会话级单 run，活跃句柄数很小，线性扫描代价可忽略。
        """
        for handle in self._handles.values():
            if handle.session_id == session_id:
                return handle
        return None

    def pop(self, run_id: str) -> RunHandle | None:
        return self._handles.pop(run_id, None)


# --------------------------------------------------------------------------- 事件出口接线


@dataclass(slots=True)
class _PendingMessage:
    """一条在途 assistant 消息的增量累积（落库时机 = message_finished 事件到达）。"""

    message_id: str
    agent: AgentRef | None
    run_id: str | None
    text: str = ""
    thinking: str = ""


class _RunEventSink:
    """RunExecutor 的 `EventSink` 实现：翻译 → emit（先落库后广播）→ assistant 行落库。

    「沿途落库」的口径：内容增量在本类内存累积（事件流仍是唯一真相），`message_finished`
    事件 emit 成功后把累积内容写一行 messages（runtime 短事务）——停止/异常路径的收口事件
    也走同一条 `emit_all`，因此在飞消息的已生成内容同样落行（AC-001-2/AC-004-1）。
    """

    def __init__(
        self,
        *,
        translator: EventTranslator,
        pipeline: EventPipeline,
        session_id: str,
        run_id: str,
        user_id: str = "local",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._translator = translator
        self._pipeline = pipeline
        self._session_id = session_id
        self._run_id = run_id
        self._user_id = user_id
        self._session_factory = session_factory
        self._pending: dict[str, _PendingMessage] = {}

    async def on_raw_event(self, raw: Any) -> None:
        """RunExecutor 的原始事件出口（A3 §6.5 强制点 4：本 sink 是 astream 的唯一解释入口）。"""
        await self.emit_all(self._translator.translate(raw))

    @property
    def model_calls(self) -> tuple[ModelCallRecord, ...]:
        """translator 的逐次模型调用记账快照（收口时逐条落 model_calls）。"""
        return self._translator.model_calls

    async def emit_all(self, events: Iterable[BaseEvent]) -> None:
        """契约事件批：逐条 emit（校验→seq→落库→广播），message_finished 随后落消息行。"""
        for event in events:
            if isinstance(event, MessageDeltaEvent):
                self._accumulate(event)
            await self._pipeline.emit(event)
            if isinstance(event, MessageFinishedEvent):
                await self._persist_closed_message(event)

    async def finish(
        self,
        phase: str,
        *,
        error: Any = None,
        usage_totals: Usage | None = None,
    ) -> None:
        """run 收口：translator.finish_run（在飞消息补 message_finished + run_status）→ emit。"""
        await self.emit_all(
            self._translator.finish_run(
                phase,  # type: ignore[arg-type]  # 调用方传 finished/stopped/error 三字面量
                error=error,
                usage_totals=usage_totals if phase == "finished" else None,
            )
        )

    # ---------------------------------------------------------------- 内部

    def _accumulate(self, event: MessageDeltaEvent) -> None:
        pending = self._pending.get(event.message_id)
        if pending is None:
            pending = _PendingMessage(
                message_id=event.message_id, agent=event.agent, run_id=event.run_id
            )
            self._pending[event.message_id] = pending
        if event.kind == "text":
            pending.text += event.delta
        else:  # thinking
            pending.thinking += event.delta

    async def _persist_closed_message(self, event: MessageFinishedEvent) -> None:
        """收口消息 → messages 行（runtime 短事务；失败记日志不拖垮事件流）。"""
        pending = self._pending.pop(event.message_id, None)
        if pending is None:
            return  # 没见过的增量（如纯审批/空工具回合）→ 无内容可落行
        if not (pending.text or pending.thinking):
            return  # 空内容消息不落行（与 translator「不发空收口」口径一致）
        row = Message(
            user_id=self._user_id,
            session_id=uuid.UUID(self._session_id),
            run_id=uuid.UUID(event.run_id) if event.run_id else None,
            role="assistant",
            agent_id=pending.agent.agent_id if pending.agent is not None else None,
            content=pending.text,
            thinking=pending.thinking or None,
            finish_reason=event.finish_reason,
            structured=event.structured.model_dump(mode="json") if event.structured else None,
        )
        factory = self._session_factory or get_session_factory()
        try:
            async with factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 —— 消息行落库失败不拖垮事件流（事件可回放重建）
            BusinessLog(
                message="assistant 消息落库失败（事件流可回放重建）",
                action="落库消息",
                entity="message",
                entity_id=event.message_id,
            ).warning(exc)


# --------------------------------------------------------------------------- 服务


class RunService(BaseService):
    """run 生命周期编排：启动（后台任务）/ 停止（幂等收敛）/ 查询 / 会话删除联动 / 沙箱投影。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        pipeline: EventPipeline,
        manager: SandboxManager,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        model_factory: ModelFactory | None = None,
        model_registry: ModelRegistry | None = None,
        active_runs: ActiveRunRegistry | None = None,
    ) -> None:
        super().__init__(db)
        self._pipeline = pipeline
        self._manager = manager
        self._checkpointer = checkpointer
        self._model_factory = model_factory
        self._model_registry = model_registry
        # 句柄表必须进程级（跨请求定位执行中的 run，见 ActiveRunRegistry docstring）
        self._active = active_runs if active_runs is not None else ActiveRunRegistry()

    # ---------------------------------------------------------------- 启动

    def start_run(self, launch: RunLaunch) -> None:
        """调度后台 run 任务（**非阻塞**：POST /messages 的 ack 不等 run）。"""
        translator = EventTranslator(session_id=launch.session_id, run_id=launch.run_id)
        sink = _RunEventSink(
            translator=translator,
            pipeline=self._pipeline,
            session_id=launch.session_id,
            run_id=launch.run_id,
            user_id=launch.user_id,
        )
        executor = RunExecutor(
            session_id=launch.session_id,
            run_id=launch.run_id,
            sink=sink,
            user_id=launch.user_id,
        )
        task = asyncio.create_task(
            self._drive(launch, executor, sink), name=f"lcode-run:{launch.run_id}"
        )
        self._active.register(
            RunHandle(
                run_id=launch.run_id, session_id=launch.session_id, executor=executor, task=task
            )
        )

    async def _drive(self, launch: RunLaunch, executor: RunExecutor, sink: _RunEventSink) -> None:
        """后台任务主体：装配 → 消费 → 收口（A3 §5.5 职责链 1-5 的编排侧）。

        **永不抛出**（后台任务无人 await 异常）：一切失败路径都收敛到「run_status(error)
        + run 行 error」，残余异常只进日志。注意沙箱拉起是同步调用（适配器面无异步 API），
        P0 取舍：阻塞事件循环的时长 ≈ 沙箱 provision 时长，真 provider 的异步化属沙箱车道。
        """
        try:
            backend = self._ensure_backend(launch)
        except Exception as exc:  # noqa: BLE001 —— 沙箱不可用按运行错误收敛（AC-002-4）
            BusinessLog(
                message="run 启动失败（沙箱装配）",
                action="启动运行",
                entity="run",
                entity_id=launch.run_id,
            ).warning(exc)
            self._active.pop(launch.run_id)
            await self._finalize(
                launch,
                RunOutcome(
                    status="error", usage=Usage(), error=classify_run_error(exc), duration_ms=0
                ),
                sink,
            )
            return

        self._manager.begin_run(launch.session_id)  # 抑制空闲 pause（Q3：run 活跃期间不暂停）
        if self._checkpointer is None:  # pragma: no cover —— dependencies 装配恒注入
            raise RuntimeError("run 启动需要 checkpointer（dependencies.get_checkpointer 注入）")
        run_task: asyncio.Task[RunOutcome] | None = None
        try:
            run_task = executor.start(
                input_messages=[HumanMessage(content=launch.content)],
                session_cfg=SessionModelCfg(
                    model=launch.model,
                    # 会话行的档位值域由 DB CHECK 约束（off/low/medium/high），收敛为契约字面量
                    reasoning_effort=cast("EffortLevel", launch.reasoning_effort),
                ),
                backend=backend,
                checkpointer=self._checkpointer,
                model_factory=self._model_factory,
            )
            outcome = await run_task
        except asyncio.CancelledError:
            # 外层任务被取消（服务关停）：让 executor 走 stopped 收敛，再按 stopped 收口
            executor.request_stop()
            outcome = RunOutcome(status="stopped", usage=Usage(), error=None, duration_ms=0)
            if run_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    outcome = await run_task
        except Exception as exc:  # noqa: BLE001 —— 兜底：executor 之外的装配/消费异常
            BusinessLog(
                message="run 执行异常",
                action="执行运行",
                entity="run",
                entity_id=launch.run_id,
            ).error(exc)
            outcome = RunOutcome(
                status="error", usage=Usage(), error=classify_run_error(exc), duration_ms=0
            )
        finally:
            self._active.pop(launch.run_id)
            with contextlib.suppress(Exception):
                self._manager.end_run(launch.session_id)

        await self._finalize(launch, outcome, sink)

    def _ensure_backend(self, launch: RunLaunch) -> Any:
        """会话沙箱就绪：在册即用（paused 先 resume）；不在册（none/killed/crashed）即拉新。"""
        session_id = launch.session_id
        client = self._manager.get(session_id)
        if client is not None:
            if self._manager.stable_phase(session_id) == "paused":
                return self._manager.resume(session_id)
            return client
        # create 允许从 none/killed/crashed 相位拉新实例（provider 用会话固化值，AC-002-6）
        return self._manager.create(session_id, provider=launch.provider)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- 停止

    async def stop_run(self, run_id: str) -> StopRunAck:
        """停止运行（AC-001-2）：返回即已收敛——事件与 run 行都已落库；幂等。"""
        row = await self._require_run(run_id)
        if row.status in ACTIVE_RUN_STATUSES:
            handle = self._active.get(run_id)
            if handle is not None:
                handle.executor.request_stop()
                await handle.task  # 收敛点：_drive 内含收口（事件 + run 行终态）
            else:
                # 进程内无句柄（服务重启后的遗留活动行）：无现场可取消。行迁移成功才
                # 补发 run_status(stopped)（F12：并发窗口内行已被其他写家收敛到终态时，
                # 不得对 error/completed 的 run 误报 stopped——按真实终态回话）
                if not await self._mark_run_row(run_id, "stopped"):
                    return await self._stop_ack_of_terminal(run_id)
                await self._pipeline.emit(
                    RunStatusEvent(
                        seq=0,
                        session_id=str(row.session_id),
                        run_id=run_id,
                        agent=None,
                        created_at=datetime.now(UTC),
                        subject="run",
                        phase="stopped",
                    )
                )
            return StopRunAck(run_id=run_id, status="stopped")
        if row.status == "stopped":
            return StopRunAck(run_id=run_id, status="stopped")  # 幂等（契约 StopRunAck 注释）
        raise RunNotActive("该运行已不在运行中", detail=f"run={run_id} status={row.status}")

    async def _stop_ack_of_terminal(self, run_id: str) -> StopRunAck:
        """无句柄兜底未迁移后的重读回话：stopped → 幂等 200；completed/error →
        RUN_NOT_ACTIVE 409；行已随会话删除 → RUN_NOT_FOUND 404（_require_run 抛出）。"""
        row = await self._require_run(run_id)
        if row.status == "stopped":
            return StopRunAck(run_id=run_id, status="stopped")
        raise RunNotActive("该运行已不在运行中", detail=f"run={run_id} status={row.status}")

    # ---------------------------------------------------------------- 查询

    async def get_run(self, run_id: str) -> RunView:
        """run 状态查询（契约无独立 run 详情端点；stop 判定与测试复用本投影）。"""
        row = await self._require_run(run_id)
        return RunView(
            run_id=str(row.id),
            session_id=str(row.session_id),
            status=row.status,
            error_code=row.error_code,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    # ---------------------------------------------------------------- 会话删除（AC-002-3）

    async def delete_session(self, session_id: str) -> None:
        """删除会话：停活动 run → kill 沙箱 → 清业务行（子表 → runs → sessions）。

        kill 失败（`SandboxError` 冒泡）时会话不删——调用方可重试，绝不出现「行没了沙箱
        还在烧钱」的状态（AC-002-3 的诚实口径）。清行走 ORM 对象删除（`session.delete`）：
        SQLAlchemy 对每行生成参数化 DELETE（占位符绑定），不拼接任何 SQL 文本；个人体量
        每会话 10^2~10^4 行，逐行删除可接受。
        """
        row = await self._require_session(session_id)
        key = row.id
        wire_id = str(key)

        # 键口径：`_active` 以 run_id 为键，本方法只有会话 id → 走值匹配定位（旧实现
        # 误用 `_active.get(session_id)` 恒为 None，导致孤儿 run 在清行后撞外键）。
        handle = self._active.get_by_session(wire_id)
        if handle is not None:  # 活动 run 先收敛（其收口事件随后随会话一起删除）
            handle.executor.request_stop()
            with contextlib.suppress(Exception):
                await handle.task

        self._manager.release(wire_id, reason="session_deleted")

        await self._purge(key)
        await self.db.flush()  # 提交交由 get_db

    async def _purge(self, key: uuid.UUID) -> None:
        """按「子表 → runs → sessions」顺序清除该会话全部业务行（ORM 参数化删除）。"""
        child_sets = (
            await self.db.scalars(select(EventRow).where(EventRow.session_id == key)),
            await self.db.scalars(select(Message).where(Message.session_id == key)),
            await self.db.scalars(select(ModelCall).where(ModelCall.session_id == key)),
            await self.db.scalars(select(SandboxUsage).where(SandboxUsage.session_id == key)),
            await self.db.scalars(select(ToolCall).where(ToolCall.session_id == key)),
            await self.db.scalars(select(Run).where(Run.session_id == key)),
        )
        for rows in child_sets:
            for child in rows:
                await self.db.delete(child)
        session_row = await self.db.get(Session, key)
        if session_row is not None:
            await self.db.delete(session_row)

    # ---------------------------------------------------------------- 沙箱投影（AC-002-6/015-4）

    async def sandbox_status(self, session_id: str) -> SandboxStatus:
        """沙箱状态投影：`stable_phase` 稳态（resumed/recreated 折叠 running）+ 费用估算。"""
        row = await self._require_session(session_id)
        wire_id = str(row.id)
        probe = self._manager.status(wire_id)
        open_segment = self._manager.open_usage_segment(wire_id)
        return SandboxStatus(
            provider=row.sandbox_provider,  # type: ignore[arg-type]  # sessions.sandbox_provider CHECK 同源
            sandbox_id=probe.sandbox_id if probe is not None else None,
            phase=self._manager.stable_phase(wire_id),
            running_since=(
                open_segment.started_at
                if open_segment is not None and open_segment.phase == "running"
                else None
            ),
            paused_since=(
                open_segment.started_at
                if open_segment is not None and open_segment.phase == "paused"
                else None
            ),
            cost_estimate=self._estimate(wire_id),
        )

    async def sandbox_estimate(self, session_id: str) -> SandboxCostEstimate:
        """会话沙箱费用估算（SessionCost.sandbox 段；running/paused 分段求和）。"""
        row = await self._require_session(session_id)
        return self._estimate(str(row.id))

    # ---------------------------------------------------------------- 内部：收口

    async def _finalize(self, launch: RunLaunch, outcome: RunOutcome, sink: _RunEventSink) -> None:
        """收口（A3 §5.5 第 5 步）：收口事件 → 逐条 model_calls → run 行终态。

        顺序不可调换：事件先落库（前端状态机信号 + 可回放），成本与审计行随后
        （失败不回滚事件）。**逐次记账**（AC-015-1）：translator 每条调用记录落一行，
        而非 run 级聚合一行——已完成调用不重复计，未记账的残差由收口对账行补齐。
        """
        try:
            await sink.finish(
                outcome.status,
                error=outcome.error,
                usage_totals=outcome.usage if outcome.status == "finished" else None,
            )
        except Exception as exc:  # noqa: BLE001 —— 收口事件失败必须留痕（F1b，不允许无日志
            # suppress）：run 行与 model_calls 仍按后续顺序收口（事件流可回放重建，
            # 成本/审计行不得因 sink 故障丢行）
            BusinessLog(
                message="run 收口事件发射失败（run 行终态与 model_calls 仍落库）",
                action="收口运行",
                entity="run",
                entity_id=launch.run_id,
            ).error(exc)

        records = sink.model_calls
        recorded_input = sum(record.input_tokens for record in records)
        recorded_output = sum(record.output_tokens for record in records)
        for record in records:  # 逐行独立 suppress（单行失败不得吞掉后续行）
            await self._record_model_call(self._model_call_entry(launch, record))

        # 残差基准的已知约束（M4）：基准是 RunOutcome.usage（runtime 侧 messages 通道的
        # 单窗口聚合），而逐次记账来自 translator（per-agent 窗口 + updates 通道）。
        # M4 并行子智能体交错流式时两种窗口口径可能结构性分叉；届时需把基准切到同源
        # 口径，或让 runtime 窗口对齐 per-agent。P0 单线执行下二者同源，对账成立。
        residual_input = max(outcome.usage.input_tokens - recorded_input, 0)
        residual_output = max(outcome.usage.output_tokens - recorded_output, 0)
        if (
            recorded_input > outcome.usage.input_tokens
            or recorded_output > outcome.usage.output_tokens
        ):
            BusinessLog(
                message="model_calls 行集 token 超过 run 终态用量，按行集为准（不静默改数）",
                action="收口运行",
                entity="run",
                entity_id=launch.run_id,
            ).warning()
        if outcome.status == "error":
            # error 路径必定恰好一条 error 行（token = 残差；已完成调用已在上面落行）
            await self._record_model_call(
                self._residual_model_call_entry(
                    launch,
                    input_tokens=residual_input,
                    output_tokens=residual_output,
                    status="error",
                    error_type=(outcome.error.code if outcome.error is not None else "internal"),
                    latency_ms=outcome.duration_ms,
                )
            )
        elif residual_input > 0 or residual_output > 0:
            BusinessLog(
                message="model_calls 行集少于 run 终态用量，补一条成功对账行",
                action="收口运行",
                entity="run",
                entity_id=launch.run_id,
            ).warning()
            await self._record_model_call(
                self._residual_model_call_entry(
                    launch,
                    input_tokens=residual_input,
                    output_tokens=residual_output,
                    status="success",
                    error_type=None,
                    latency_ms=0,
                )
            )

        await self._close_run_row(launch.run_id, outcome)

    async def _record_model_call(self, entry: ModelCallEntry) -> None:
        """写一行 model_calls（短事务）；单行失败只抑制本行（调用方逐条调用）。"""
        with contextlib.suppress(Exception):
            await record_model_call_short_tx(entry)

    def _model_call_entry(self, launch: RunLaunch, record: ModelCallRecord) -> ModelCallEntry:
        """translator 记账 → model_calls 行：role 映射 + 模型路由 + 延迟/起止时刻。

        agent_id 归一：role 为 None/"main" = 主智能体（其记录可能带内层 deepagents 的
        `execute:<uuid>` 命名空间路径——那是执行容器而非子智能体身份），落库为 NULL；
        子智能体记录（含 general-purpose）带 role，保留自己的 agent_id。
        """
        role = _MODEL_CALL_ROLE_MAP.get(record.role or "", "main")
        is_main_agent = record.role is None or record.role == "main"
        agent_id = None if is_main_agent else record.agent_id
        model_id = self._resolve_model_id(role, launch)
        usage = Usage(input_tokens=record.input_tokens, output_tokens=record.output_tokens)
        return ModelCallEntry(
            session_id=launch.session_id,
            run_id=launch.run_id,
            model=model_id,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost=self._model_cost(model_id, usage),
            agent_id=agent_id,
            role=role,
            latency_ms=max(int((record.finished_at - record.started_at).total_seconds() * 1000), 0),
            status="success",
            user_id=launch.user_id,
            created_at=record.started_at,
        )

    def _residual_model_call_entry(
        self,
        launch: RunLaunch,
        *,
        input_tokens: int,
        output_tokens: int,
        status: Literal["success", "error"],
        error_type: str | None,
        latency_ms: int,
    ) -> ModelCallEntry:
        """残差对账行（run 级口径：main 角色、无 agent、按会话模型计价）。"""
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
        return ModelCallEntry(
            session_id=launch.session_id,
            run_id=launch.run_id,
            model=launch.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=self._model_cost(launch.model, usage),
            agent_id=None,
            role="main",
            latency_ms=latency_ms,
            status=status,
            error_type=error_type,
            user_id=launch.user_id,
        )

    def _resolve_model_id(self, role: str, launch: RunLaunch) -> str:
        """按角色解析模型 id（会话级模型覆盖角色级，故通常 = launch.model）。

        注册表缺失/解析失败 → 回退 launch.model 并记 warning（不丢行、不中断收口）。
        """
        if self._model_registry is None:
            BusinessLog(
                message="模型注册表缺失，model_calls 按会话模型记",
                action="解析模型路由",
                entity="model",
                entity_id=launch.model,
            ).warning()
            return launch.model
        try:
            resolved = self._model_registry.resolve(
                role,
                session_cfg=SessionModelCfg(
                    model=launch.model,
                    reasoning_effort=cast("EffortLevel", launch.reasoning_effort),
                ),
            )
        except Exception as exc:  # noqa: BLE001 —— 路由失败降级，不拖垮 run 收口
            BusinessLog(
                message="模型路由解析失败，model_calls 按会话模型记",
                action="解析模型路由",
                entity="model",
                entity_id=launch.model,
            ).warning(exc)
            return launch.model
        return resolved.spec.id

    def _model_cost(self, model_id: str, usage: Usage) -> float:
        """按注册表单价估算；未知模型降级为 0（token 照常落库，cost_service 同口径）。"""
        if self._model_registry is None:
            return 0.0
        try:
            pricing = self._model_registry.pricing(model_id)
        except Exception as exc:  # noqa: BLE001 —— 单价缺失降级，不拖垮 run 收口
            BusinessLog(
                message="模型单价缺失，费用按 0 记",
                action="估算模型成本",
                entity="model",
                entity_id=model_id,
            ).warning(exc)
            return 0.0
        return model_cost_of(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_per_mtok=pricing.input_per_mtok,
            output_per_mtok=pricing.output_per_mtok,
        )

    async def _close_run_row(self, run_id: str, outcome: RunOutcome) -> None:
        """run 行终态（runtime 短事务）；行已不存在（会话被删）→ 记日志静默返回。"""
        status = _OUTCOME_TO_RUN_STATUS[outcome.status]
        factory = get_session_factory()
        try:
            async with factory() as session:
                row = await session.get(Run, uuid.UUID(run_id))
                if row is None:
                    BusinessLog(
                        message="run 行已不存在（会话被删除），跳过终态收口",
                        action="收口运行",
                        entity="run",
                        entity_id=run_id,
                    ).warning()
                    return
                row.status = status
                row.finished_at = datetime.now(UTC)
                if outcome.error is not None:
                    row.error_code = outcome.error.code
                    row.error_message = outcome.error.message
                await session.commit()
        except Exception as exc:  # noqa: BLE001 —— 终态落行失败只记日志（事件已可回放）
            BusinessLog(
                message="run 行终态落库失败",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).error(exc)

    async def _mark_run_row(self, run_id: str, status: str) -> bool:
        """无句柄场景的终态兜底（status ∈ completed/stopped/error）；仅在行仍处活动态时
        迁移并返回 True——行缺失/已终态返回 False（调用方不得对未迁移的行补发事件）。"""
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(Run, uuid.UUID(run_id))
            if row is None or row.status not in ACTIVE_RUN_STATUSES:
                return False
            row.status = status
            row.finished_at = datetime.now(UTC)
            await session.commit()
            return True

    # ---------------------------------------------------------------- 内部：查询

    def _estimate(self, wire_id: str) -> SandboxCostEstimate:
        # manager.usage_segments 返回的是「分段记录」（含 provider/sandbox_id 元数据），
        # 估算器只吃分段快照——取 .segment 投影
        segments = [record.segment for record in self._manager.usage_segments(wire_id)]
        return estimate_sandbox_cost(
            segments,
            open_segment=self._manager.open_usage_segment(wire_id),
        )

    async def _require_run(self, run_id: str) -> Run:
        try:
            key = uuid.UUID(run_id)
        except ValueError as exc:
            raise RunNotFound(
                f"运行不存在：{run_id}", detail="路径参数不是合法 UUID", original_exception=exc
            ) from exc
        row = await self.db.scalar(select(Run).where(Run.id == key))
        if row is None:
            raise RunNotFound(f"运行不存在：{run_id}")
        return row

    async def _require_session(self, session_id: str) -> Session:
        try:
            key = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFound(
                f"会话不存在：{session_id}", detail="路径参数不是合法 UUID", original_exception=exc
            ) from exc
        row = await self.db.scalar(select(Session).where(Session.id == key))
        if row is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        return row


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ActiveRunRegistry",
    "RunHandle",
    "RunService",
    "RunView",
]
