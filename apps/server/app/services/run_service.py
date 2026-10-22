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
- **审批挂起 / resume（M3 Phase 1）**：段末 `waiting_approval` 时把 interrupt 映射成
  approvals pending 行 + `approval_request` 事件 + 播种上下文（进程级 registry），
  run 行置 waiting_approval 而不收口；决策齐备后 `resume_run` 用**同 run_id/thread_id**
  新建 executor，输入 `Command(resume=...)` 续跑最终段（D4）。审批模式固定为 run 启动
  快照值（D5：避免决策窗口内 when 翻转）。

事务边界（A3 §7.3）：本服务有两类上下文——请求级 `db`（stop/查询/删除：只 flush，提交在
get_db）；后台任务与收口（事件 emit、model_calls、assistant 消息行、run 行终态、审批行：
自取 `core/db` 短会话立即 commit——run 不是请求，绝不长事务占连接）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.messages import HumanMessage
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.auto_approval import AutoApprovalAuditSink, AutoApprovalRecord
from app.agent.models_registry import SessionModelCfg
from app.agent.permissions import approval_reason, risk_level_for
from app.agent.runtime import InterruptEntry, RunExecutor, RunOutcome
from app.agent.state import SessionCfg
from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events import EventPipeline, EventTranslator, ModelCallRecord
from app.events.translator import ApprovalTarget, InFlightToolCall, InterruptMappingError
from app.exception.common import InvalidArgument
from app.exception.run import RunNotActive, RunNotFound
from app.exception.session import SessionNotFound
from app.models import (
    Approval,
    DiffReview,
    GitBaseline,
    Message,
    ModelCall,
    Run,
    SandboxUsage,
    Session,
    ToolCall,
)
from app.models import Event as EventRow
from app.sandbox.manager import SandboxManager
from app.services.approval_service import (
    ApprovalService,
    PendingApproval,
    ResumeDecision,
    create_pending_batch_short_tx,
    record_auto_approval_short_tx,
)
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

DiffRunner = Callable[["RunLaunch"], Awaitable[None]]
"""run 正常收口后的 diff 就绪钩子（实现 = `diff_service.prepare_review_short_tx`）。

注入制而非直接 import：run_service 不持有 diff 域细节；unit 层构造不传即为 no-op
（零库零沙箱），生产由 `dependencies.get_run_service` 装配。**失败隔离**：钩子抛错只记
ERROR，run 已完成的终态与事件绝不因此改写（diff 是产物，不是运行结果）。"""

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
    """一条在跑 run 的进程内句柄（stop 的操作面；run 结束即摘除）。

    `task` 的类型随车道不同：首段是 `_drive` 的 Task[None]，resume 段是 executor
    的任务 Task[RunOutcome]——stop 只 `await` 它等收口，不消费返回值，故取 Any。
    """

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        executor: RunExecutor,
        task: asyncio.Task[Any],
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

    `claim_resume` 是 resume 段的**幂等闸**：审批齐备时多个请求可能同时判定「已全部
    决策」，只允许第一个抢占者调度 resume；抢占在 `_drive_resume` 结束时释放。
    """

    def __init__(self) -> None:
        self._handles: dict[str, RunHandle] = {}
        self._resuming: set[str] = set()

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

    def claim_resume(self, run_id: str) -> bool:
        """抢占一次 resume 调度权（已有在跑句柄或已在 resume 时返回 False）。"""
        if run_id in self._resuming or run_id in self._handles:
            return False
        self._resuming.add(run_id)
        return True

    def release_resume(self, run_id: str) -> None:
        self._resuming.discard(run_id)


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """一次审批挂起的续跑上下文（进程级登记；跨请求 resume 的播种锚）。

    - `launch`：run 启动快照（approval_mode 在此固化——D5 的 when 不翻转保证）；
    - `batch_tool_call_ids`：**当前挂起批次**的 tool_call_id（顺序 = 挂起顺序）——
      HITL resume 的决策数与顺序必须与它一致（多/少都会让 middleware 抛错）；
    - `in_flight_tool_calls`：全 run 仍在飞的工具调用（富快照）——resume 段播种后
      不重发 started，`task` 调用还能重建 task 栈（子智能体事件归属）；
    - `closed_tool_call_ids`：本 run 已收口的工具调用 id——resume 段图会重放整段历史，
      不播种会让历史里的 finished 调用二次公告 started/finished（同 run 重复工具卡）；
    - `message_ids`：本 run 已收口的消息上游 id——execute 节点最终更新会重发整段
      历史，不播种会重复 message_finished / 模型调用记账（token 双计）。
    """

    launch: RunLaunch
    batch_tool_call_ids: tuple[str, ...]
    in_flight_tool_calls: tuple[InFlightToolCall, ...]
    closed_tool_call_ids: tuple[str, ...]
    message_ids: tuple[str, ...]


class ApprovalResumeRegistry:
    """进程级 resume 上下文表（waiting 段写入，resume 段读取；进程重启即清空）。

    重启后仍可 resume（PG checkpointer 承载断点），但播种信息丢失——届时会退化为
    「resume 段重发挂起回合的 started/finished」（run 仍可完成，事件存在重复）。
    这是本阶段不落库（契约零变更）的已知边界，记在 m3 计划的风险项里。
    """

    def __init__(self) -> None:
        self._contexts: dict[str, ResumeContext] = {}

    def put(self, run_id: str, context: ResumeContext) -> None:
        self._contexts[run_id] = context

    def get(self, run_id: str) -> ResumeContext | None:
        return self._contexts.get(run_id)

    def pop(self, run_id: str) -> ResumeContext | None:
        return self._contexts.pop(run_id, None)


# --------------------------------------------------------------------------- 事件出口接线


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    """后台 resume 任务的未检索异常兜底（任务本身不 await）。"""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        exc = task.exception()
        if isinstance(exc, Exception):
            BusinessLog(
                message="审批恢复后台任务异常（已由任务内收口处理）",
                action="恢复审批运行",
                entity="run",
            ).error(exc)


def _decisions_for_current_interrupt(
    context: ResumeContext | None, decisions: Sequence[ResumeDecision]
) -> list[ResumeDecision]:
    """按**当前 interrupt** 的 tool_call_ids 重排/裁剪 resume 决策（顺序 = 挂起顺序）。

    为什么不能直接透传 service 的决策列表：service 返回该 run 的全部已决审批（审计口径），
    而 HITL resume 只接受**本次挂起**的那一批——多一批少一批都会让
    `HumanInTheLoopMiddleware` 以「决策数 ≠ 挂起调用数」抛 ValueError（本车道实测）。
    `ResumeContext.batch_tool_call_ids` 是「当前挂起批次」的权威来源（park 时写入）。

    缺决策（挂起调用没有对应决定）→ 抛 InvalidArgument（fail-closed：绝不补默认放行）。
    上下文缺失（进程重启）→ 原样透传（已知降界，见 ApprovalResumeRegistry docstring）。
    """
    if context is None or not context.batch_tool_call_ids:
        return list(decisions)
    by_id = {decision.tool_call_id: decision for decision in decisions}
    missing = [call_id for call_id in context.batch_tool_call_ids if call_id not in by_id]
    if missing:
        raise InvalidArgument("审批决策不齐，无法恢复运行", detail=f"缺少决策的挂起调用：{missing}")
    ordered = [by_id[call_id] for call_id in context.batch_tool_call_ids]
    extra = len(decisions) - len(ordered)
    if extra > 0:
        BusinessLog(
            message="resume 决策多于当前挂起批次（多余决策已忽略）",
            action="恢复审批运行",
            entity="run",
        ).warning()
    return ordered


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
        self._model_calls_recorded = False

    async def on_raw_event(self, raw: Any) -> None:
        """RunExecutor 的原始事件出口（A3 §6.5 强制点 4：本 sink 是 astream 的唯一解释入口）。"""
        await self.emit_all(self._translator.translate(raw))

    @property
    def translator(self) -> EventTranslator:
        """本段翻译器（收口时审批映射 `describe_interrupt` 的唯一入口）。"""
        return self._translator

    @property
    def model_calls(self) -> tuple[ModelCallRecord, ...]:
        """translator 的逐次模型调用记账快照（收口时逐条落 model_calls）。"""
        return self._translator.model_calls

    @property
    def model_calls_recorded(self) -> bool:
        """本段 model_calls 是否已落行（同一 sink 只允许落一次）。"""
        return self._model_calls_recorded

    def mark_model_calls_recorded(self) -> None:
        """标记本段记账已执行（`_finalize` 的 error 路径会复用同一 sink，必须幂等）。"""
        self._model_calls_recorded = True

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
        approval_resume: ApprovalResumeRegistry | None = None,
        diff_runner: DiffRunner | None = None,
    ) -> None:
        super().__init__(db)
        self._pipeline = pipeline
        self._manager = manager
        self._checkpointer = checkpointer
        self._model_factory = model_factory
        self._model_registry = model_registry
        # 句柄表必须进程级（跨请求定位执行中的 run，见 ActiveRunRegistry docstring）
        self._active = active_runs if active_runs is not None else ActiveRunRegistry()
        # resume 上下文同样必须进程级（waiting 在 A 请求收口、决策在 B 请求 resume）
        self._resume = approval_resume if approval_resume is not None else ApprovalResumeRegistry()
        # diff 就绪钩子（M3 Phase 4）：None = 不计算 diff（unit 层默认；生产由 dependencies 注入）
        self._diff_runner = diff_runner

    # ---------------------------------------------------------------- 启动

    @staticmethod
    def _auto_audit_sink(launch: RunLaunch) -> AutoApprovalAuditSink:
        """AUTO 分类器的审计落行回调（短事务、逐条 commit；AC-013-3）。

        middleware 在 agent 执行路径内，不得持有长事务/长连接：本回调只为一次
        auto_approve 决策开一个短会话并立即提交（与 `_park_for_approval` 的短事务
        口径一致）。落行失败由 middleware 捕获并记 ERROR，不阻断工具执行。
        幂等由 `record_auto_approval_short_tx` 的 (session, run, tool_call_id) 判重保证。
        """

        async def record(records: Sequence[AutoApprovalRecord]) -> None:
            for record in records:
                await record_auto_approval_short_tx(
                    session_id=launch.session_id,
                    run_id=launch.run_id,
                    user_id=launch.user_id,
                    item=PendingApproval(
                        tool_call_id=record.tool_call_id,
                        tool=record.tool,
                        args=record.args,
                        risk_level=record.risk_level,
                        reason=record.reason,
                    ),
                    reason=record.reason,
                )

        return record

    def start_run(self, launch: RunLaunch) -> None:
        """调度后台 run 任务（**非阻塞**：POST /messages 的 ack 不等 run）。"""
        translator = self._build_translator(launch)
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
            approval_mode=launch.approval_mode,
            sandbox_provider=launch.provider,
            auto_audit=self._auto_audit_sink(launch),
        )
        task = asyncio.create_task(
            self._drive(launch, executor, sink), name=f"lcode-run:{launch.run_id}"
        )
        self._active.register(
            RunHandle(
                run_id=launch.run_id, session_id=launch.session_id, executor=executor, task=task
            )
        )

    @staticmethod
    def _build_translator(
        launch: RunLaunch,
        *,
        resumed_tool_calls: Sequence[InFlightToolCall] = (),
        resumed_closed_tool_call_ids: Sequence[str] = (),
        resumed_message_ids: Sequence[str] = (),
    ) -> EventTranslator:
        """本段翻译器：注入风险/理由解析器（审批卡片）+ resume 播种（去重锚）。"""
        return EventTranslator(
            session_id=launch.session_id,
            run_id=launch.run_id,
            risk_level_for=risk_level_for,
            approval_reason=approval_reason,
            resumed_tool_calls=resumed_tool_calls,
            resumed_closed_tool_call_ids=resumed_closed_tool_call_ids,
            resumed_message_ids=resumed_message_ids,
        )

    async def _drive(
        self,
        launch: RunLaunch,
        executor: RunExecutor,
        sink: _RunEventSink,
    ) -> None:
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
        interrupts: tuple[InterruptEntry, ...] = ()
        try:
            run_task = executor.start(
                input_messages=[HumanMessage(content=launch.content)],
                session_cfg=self._session_cfg_of(launch),
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
            interrupts = executor.interrupts

        await self._finalize(launch, outcome, sink, interrupts=interrupts)

    @staticmethod
    def _session_cfg_of(launch: RunLaunch) -> SessionCfg:
        """会话快照（state 形态）：模型/档位/审批模式/provider 全部固化进 checkpoint。"""
        return SessionCfg(
            model=launch.model,
            reasoning_effort=launch.reasoning_effort,
            approval_mode=launch.approval_mode,
            sandbox_provider=launch.provider,
        )

    # ---------------------------------------------------------------- 审批 resume（M3 D4）

    def resume_run(self, run_id: str, decisions: Sequence[ResumeDecision]) -> None:
        """调度 resume 段（**非阻塞**：审批决策 ack 不等本轮执行完）。

        同 run_id/thread_id 新建 executor：输入 `Command(resume={"decisions": [...]})`。
        `approval_mode` 固定为 run 启动快照值（决策窗口内会话模式变化不影响本轮策略）。
        同一 run 只允许一次在途 resume（`ActiveRunRegistry.claim_resume`）——重复调度
        静默丢弃并记日志（决策行已落，重复 resume 会让图状态错位）。
        """
        if not decisions:
            raise InvalidArgument("resume 决策为空", detail="没有可恢复的审批决策")
        if not self._active.claim_resume(run_id):
            BusinessLog(
                message="该 run 已在执行或已在 resume 中，忽略重复调度",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).warning()
            return
        task = asyncio.create_task(
            self._drive_resume(run_id, list(decisions)), name=f"lcode-resume:{run_id}"
        )
        # 句柄在 _drive_resume 内注册（executor 是异步装配的）；这里只兜住未检索异常
        task.add_done_callback(_log_task_failure)

    async def _drive_resume(self, run_id: str, decisions: list[ResumeDecision]) -> None:
        """resume 段主体：读会话行 → 装配（播种锚）→ 消费 → 收口（**永不抛出**）。"""
        sink: _RunEventSink | None = None
        try:
            launch, context = await self._load_resume_launch(run_id)
            if launch is None:
                return
            await self._mark_run_running(run_id)
            translator = self._build_translator(
                launch,
                resumed_tool_calls=context.in_flight_tool_calls if context else (),
                resumed_closed_tool_call_ids=context.closed_tool_call_ids if context else (),
                resumed_message_ids=context.message_ids if context else (),
            )
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
                approval_mode=launch.approval_mode,
                sandbox_provider=launch.provider,
                auto_audit=self._auto_audit_sink(launch),
            )
            backend = self._ensure_backend(launch)
            self._manager.begin_run(launch.session_id)
            if self._checkpointer is None:  # pragma: no cover —— dependencies 恒注入
                raise RuntimeError("resume 需要 checkpointer（dependencies.get_checkpointer）")
            task = executor.start(
                input_messages=[],
                session_cfg=self._session_cfg_of(launch),
                backend=backend,
                checkpointer=self._checkpointer,
                model_factory=self._model_factory,
                resume_value={
                    "decisions": [
                        decision.to_hitl_decision()
                        for decision in _decisions_for_current_interrupt(context, decisions)
                    ]
                },
            )
            self._active.register(
                RunHandle(
                    run_id=launch.run_id,
                    session_id=launch.session_id,
                    executor=executor,
                    task=task,
                )
            )
            interrupts: tuple[InterruptEntry, ...] = ()
            try:
                outcome = await task
            finally:
                self._active.pop(run_id)
                with contextlib.suppress(Exception):
                    self._manager.end_run(launch.session_id)
                interrupts = executor.interrupts
        except Exception as exc:  # noqa: BLE001 —— resume 装配/执行失败按 run error 收敛
            BusinessLog(
                message="resume 段执行异常",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).error(exc)
            await self._fail_run(run_id, classify_run_error(exc, message="审批恢复执行失败"))
            return
        finally:
            self._active.release_resume(run_id)

        # 续跑后可能再次挂起（多批审批）或走到最终段
        await self._finalize(launch, outcome, sink, interrupts=interrupts)

    async def _load_resume_launch(
        self, run_id: str
    ) -> tuple[RunLaunch | None, ResumeContext | None]:
        """读 resume 入参：会话行（模型/档位/provider）+ 启动快照（approval_mode）。

        `approval_mode` 取 run 启动快照（`ResumeContext`）；进程重启丢了上下文时回落
        会话行当前值并记 warning（播种信息同时丢失——事件可能重复，run 仍可完成）。
        """
        try:
            key = uuid.UUID(run_id)
        except ValueError:
            BusinessLog(
                message="resume 的 run_id 非法，忽略",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).warning()
            return None, None
        context = self._resume.get(run_id)
        async with get_session_factory()() as session:
            run_row = await session.get(Run, key)
            if run_row is None:
                BusinessLog(
                    message="resume 时 run 行不存在（会话已删），忽略",
                    action="恢复审批运行",
                    entity="run",
                    entity_id=run_id,
                ).warning()
                return None, None
            if run_row.status != "waiting_approval":
                BusinessLog(
                    message="resume 时 run 不在 waiting_approval 态，忽略",
                    action="恢复审批运行",
                    entity="run",
                    entity_id=run_id,
                ).warning()
                return None, None
            session_row = await session.get(Session, run_row.session_id)
            if session_row is None:  # pragma: no cover —— FK 保证
                return None, None
            approval_mode = (
                context.launch.approval_mode if context is not None else session_row.approval_mode
            )
            if context is None:
                BusinessLog(
                    message="resume 上下文缺失（进程重启），按会话行审批模式续跑（事件可能重复）",
                    action="恢复审批运行",
                    entity="run",
                    entity_id=run_id,
                ).warning()
            launch = RunLaunch(
                session_id=str(session_row.id),
                run_id=run_id,
                content="",
                model=session_row.model,
                reasoning_effort=session_row.reasoning_effort,
                provider=session_row.sandbox_provider,
                approval_mode=approval_mode,
                user_id=session_row.user_id,
            )
        return launch, context

    async def _mark_run_running(self, run_id: str) -> None:
        """resume 段开始：run 行从 waiting_approval 迁回 running（stop 语义可生效）。"""
        async with get_session_factory()() as session:
            row = await session.get(Run, uuid.UUID(run_id))
            if row is not None and row.status == "waiting_approval":
                row.status = "running"
                await session.commit()

    async def _fail_run(self, run_id: str, error: Any) -> None:
        self._resume.pop(run_id)
        """resume 装配失败的最小收口：run 行 error + run_status(error) 事件。

        sink/translator 尚未建立（或已丢失）时不能走 `_finalize`——这里只补 run 行与
        终态事件（已落库的事件仍可回放），run 不会永远卡在 waiting_approval。
        """
        try:
            async with get_session_factory()() as session:
                row = await session.get(Run, uuid.UUID(run_id))
                if row is None:
                    return
                session_id = str(row.session_id)
                row.status = "error"
                row.finished_at = datetime.now(UTC)
                row.error_code = getattr(error, "code", "internal")
                row.error_message = getattr(error, "message", "运行失败")
                await session.commit()
            await self._pipeline.emit(
                RunStatusEvent(
                    seq=0,
                    session_id=session_id,
                    run_id=run_id,
                    agent=None,
                    created_at=datetime.now(UTC),
                    subject="run",
                    phase="error",
                    error=error,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底收口失败只记日志
            BusinessLog(
                message="resume 失败收口落库失败",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).error(exc)

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
        """停止运行（AC-001-2）：返回即已收敛——事件与 run 行都已落库；幂等。

        审批挂起态（M3 D3）：先 `close_pending_for_run` 把 pending 审批置 rejected
        （decided_by=system）并广播 approval_resolved，再收敛 run——挂起卡片不会
        在停止后继续可点（点击会命中 409）。
        """
        row = await self._require_run(run_id)
        if row.status in ACTIVE_RUN_STATUSES:
            if row.status == "waiting_approval":
                await self._close_pending_approvals(run_id)
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
            self._resume.pop(run_id)  # 停止后不再保留 resume 播种（迟到决策会被 409 拦下）
            return StopRunAck(run_id=run_id, status="stopped")
        if row.status == "stopped":
            return StopRunAck(run_id=run_id, status="stopped")  # 幂等（契约 StopRunAck 注释）
        raise RunNotActive("该运行已不在运行中", detail=f"run={run_id} status={row.status}")

    async def _close_pending_approvals(self, run_id: str) -> None:
        """stop 清理（短事务在此服务内自持请求会话：stop 是请求路径，提交由本方法显式做）。"""
        service = ApprovalService(self.db, pipeline=self._pipeline)
        with contextlib.suppress(Exception):
            await service.close_pending_for_run(run_id, feedback="运行已停止，审批自动驳回")

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

        并发写入者（审批停车路径 / resume 段）可能在「选完子表」与「删会话行」之间插入
        新子表行，令会话删除撞外键——`_purge_with_retry` 用有界重试收口（不加锁：两边
        都跨 sessions + runs 的 FK 校验，任何单边加锁都会与对方成环死锁）。
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

        await self._purge_with_retry(key)

    async def _purge_with_retry(self, key: uuid.UUID) -> None:
        """清业务行（含并发写入者的有界重试；提交仍交由 get_db）。

        典型并发方：审批停车路径在 run 已 `waiting_approval` 之后仍会写 model_calls /
        approval_request 事件（`_park_for_approval`），resume 段也在写事件。它们的外键
        校验会锁 sessions + runs 两行，与本方法「选子表 → 删会话」之间没有单一加锁顺序
        （任何单边加锁 ↔ 对方 FK 行锁会成环），所以用重试而不是锁：撞外键就回滚重来
        （重选能看到新行），最多 3 次。P0 体量下重试窗口是毫秒级。
        """
        for attempt in range(3):
            try:
                await self._purge(key)
                await self.db.flush()  # 提交交由 get_db
                return
            except IntegrityError:
                await self.db.rollback()
                if attempt == 2:
                    raise
                await asyncio.sleep(0.02)

    async def _purge(self, key: uuid.UUID) -> None:
        """按「子表 → runs → sessions」顺序清除该会话全部业务行（ORM 参数化删除）。

        M3 起 approvals / diff_reviews / git_baselines 也是会话子表（`session_id` 外键），
        且 **approvals 还引用 runs**——审批落行后若不先删这三表，删 runs 必撞外键
        （m3-spikes §5 第 5 条）。
        """
        child_sets = (
            await self.db.scalars(select(EventRow).where(EventRow.session_id == key)),
            await self.db.scalars(select(Message).where(Message.session_id == key)),
            await self.db.scalars(select(ModelCall).where(ModelCall.session_id == key)),
            await self.db.scalars(select(SandboxUsage).where(SandboxUsage.session_id == key)),
            await self.db.scalars(select(ToolCall).where(ToolCall.session_id == key)),
            await self.db.scalars(select(Approval).where(Approval.session_id == key)),
            await self.db.scalars(select(DiffReview).where(DiffReview.session_id == key)),
            await self.db.scalars(select(GitBaseline).where(GitBaseline.session_id == key)),
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

    async def _finalize(
        self,
        launch: RunLaunch,
        outcome: RunOutcome,
        sink: _RunEventSink,
        *,
        interrupts: Sequence[InterruptEntry] = (),
    ) -> None:
        """收口（A3 §5.5 第 5 步）：收口事件 → 逐条 model_calls → run 行终态。

        顺序不可调换：事件先落库（前端状态机信号 + 可回放），成本与审计行随后
        （失败不回滚事件）。**逐次记账**（AC-015-1）：translator 每条调用记录落一行，
        而非 run 级聚合一行——已完成调用不重复计，未记账的残差由收口对账行补齐。

        审批挂起（M3 D4）：`waiting_approval` 走**停车**分支——不调 `sink.finish`、
        不发 run_status(finished)、run 行置 waiting_approval（无 finished_at）；
        run 行挂起态、审批行、resume 播种与 model_calls 全部先落库，**最后**才发
        `approval_request`（事件可见即代表客户端可立即决策续跑），最终段才收口。
        映射失败（interrupt 对不上在飞调用）→ 转 error 收敛（fail-closed，绝不自动放行）。
        """
        if outcome.status == "waiting_approval":
            try:
                await self._park_for_approval(launch, sink, interrupts)
                return
            except Exception as exc:  # noqa: BLE001 —— 映射失败必须让 run 以 error 收敛
                BusinessLog(
                    message="审批挂起映射失败（run 按 error 收敛，不自动放行）",
                    action="收口运行",
                    entity="run",
                    entity_id=launch.run_id,
                ).error(exc)
                outcome = RunOutcome(
                    status="error",
                    usage=outcome.usage,
                    error=classify_run_error(
                        exc if isinstance(exc, Exception) else RuntimeError("审批映射失败"),
                        message="审批挂起映射失败，运行已终止",
                    ),
                    duration_ms=outcome.duration_ms,
                )
                # 映射/事件失败前可能已落 pending 行：一并关掉（否则挂起卡片永远可点，
                # run 却已 error）。失败只记日志，不掩盖原始错误。
                await self._close_pending_short_tx(
                    launch.run_id, feedback="审批挂起失败，运行已终止"
                )

        usage_totals: Usage | None = None
        if outcome.status == "finished":
            # 跨段合计（D4）：最终段的 usage_totals = 该 run 既往 model_calls 合计 +
            # 本段 usage（resume 前的段落已落行；只算最后一段会少计）。
            prior = await self._prior_model_usage(launch.run_id)
            usage_totals = Usage(
                input_tokens=prior.input_tokens + outcome.usage.input_tokens,
                output_tokens=prior.output_tokens + outcome.usage.output_tokens,
            )
        self._resume.pop(launch.run_id)  # 终态段不再需要 resume 播种（含 error/stopped）

        try:
            await sink.finish(
                outcome.status,
                error=outcome.error,
                usage_totals=usage_totals,
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

        await self._record_segment_model_calls(launch, outcome, sink)
        await self._close_run_row(launch.run_id, outcome)
        if outcome.status == "finished":
            # 顺序钉死：run_status(finished) → model_calls → run 行终态 → diff 就绪。
            # diff 在**收口之后**计算（事件/终态都已提交），耗时只延长后台任务寿命，
            # 不影响 run 终态事件（详见 _prepare_diff_review 的失败隔离）。
            await self._prepare_diff_review(launch)

    async def _prepare_diff_review(self, launch: RunLaunch) -> None:
        """diff 就绪钩子（M3 Phase 4，REQ-007）：**失败隔离**。

        diff 是 run 的产物而非运行结果：`prepare_review` 的任何失败（沙箱不可用、git
        命令失败、写库失败、事件发射失败）都只记 ERROR 并以「无 diff」收敛——run 仍是
        `completed`，`run_status(finished)` 与 run 行终态绝不因此改写。
        """
        if self._diff_runner is None:
            return
        try:
            await self._diff_runner(launch)
        except Exception as exc:  # noqa: BLE001 —— 产物失败不得改写运行终态
            BusinessLog(
                message="diff 审查准备失败（run 已完成，按无 diff 收敛）",
                action="准备diff审查",
                entity="run",
                entity_id=launch.run_id,
            ).error(exc)

    async def _park_for_approval(
        self,
        launch: RunLaunch,
        sink: _RunEventSink,
        interrupts: Sequence[InterruptEntry],
    ) -> None:
        """审批挂起停车：映射 → run 行挂起 → model_calls → pending 行 → 播种 → 事件。

        顺序（M3 生产时序修复；每个「客户端可观察的事实」都晚于它依赖的全部事实）：

        1. **run 行先迁到 `waiting_approval` 并 commit**——这是客户端可决策的前置条件
           （`ApprovalService._start_resume` 的守卫只认 waiting_approval）；
        2. 记本段 model_calls——必须早于审批行 commit：决策可能瞬间触发 resume 并跑完
           最终段，收口算跨段 `usage_totals` 时要能看到本段已落行（否则少计首段 token；
           轮询审批列表的客户端不等事件，所以这条也不能晚于审批行）；
        3. pending 审批行短事务 commit（`create_pending_batch_short_tx`）——卡片行因此
           永远晚于 run 挂起态与成本行出现；
        4. 播种 resume 上下文（进程级 registry）——事件到达后的最快决策立刻能读到
           完整播种，不会退化成「重发挂起回合」的降级路径；
        5. 最后逐条 emit `approval_request`（事件只引用已提交事实）。

        旧顺序（审批行 → emit → run 行挂起 → 播种）有真实竞态：客户端收到事件后立即
        决策时 run 行仍是 running，`_start_resume` 跳过 resume、审批行已 approved，
        重试决策得 409 → run 永久卡在 waiting_approval。e2e 曾用轮询 run_state 规避，
        本修复把窗口从生产路径移除（广播时 run 已可续跑）。

        映射为空（没有根层 interrupt / 没有 action_requests）视为失败：抛
        `InterruptMappingError` 由 `_finalize` 收为 error。
        """
        translator = sink.translator
        targets: list[ApprovalTarget] = []
        seen: set[str] = set()
        for namespace, interrupt in interrupts:
            for target in translator.describe_interrupt(namespace, interrupt):
                if target.tool_call_id in seen:
                    continue
                seen.add(target.tool_call_id)
                targets.append(target)
        if not targets:
            raise InterruptMappingError(
                "运行以 interrupt 挂起，但未解析到任何待审批工具调用（fail-closed）"
            )

        if not await self._set_run_waiting(launch.run_id):
            # 行已随会话删除，或已被并发 stop 收口到终态：不得再发 approval_request
            # （否则留下「可点但永远 resume 不了」的卡片）。run 行终态归抢先方所有。
            return

        await self._record_segment_model_calls(launch, None, sink)

        items = await create_pending_batch_short_tx(
            session_id=launch.session_id,
            run_id=launch.run_id,
            user_id=launch.user_id,
            items=[
                PendingApproval(
                    tool_call_id=target.tool_call_id,
                    tool=target.tool,
                    args=target.args,
                    risk_level=target.risk_level,
                    reason=target.reason,
                )
                for target in targets
            ],
        )
        if not await self._run_still_waiting(launch.run_id):
            # stop 恰好落在「run 挂起」与「审批行落库」两次提交之间：run 已终态，
            # 刚落的卡片必须立刻按 system 驳回（否则客户端会看到永远点不动的卡片）。
            await self._close_pending_short_tx(launch.run_id, feedback="运行已结束，审批自动驳回")
            return

        self._resume.put(
            launch.run_id,
            ResumeContext(
                launch=launch,
                batch_tool_call_ids=tuple(target.tool_call_id for target in targets),
                in_flight_tool_calls=translator.open_tool_calls,
                closed_tool_call_ids=translator.closed_tool_call_ids,
                message_ids=translator.handled_message_ids,
            ),
        )
        for item, target in zip(items, targets, strict=True):
            await self._pipeline.emit(translator.approval_request_event(item.id, target))

    async def _close_pending_short_tx(self, run_id: str, *, feedback: str) -> None:
        """短事务关闭该 run 的 pending 审批（挂起失败/终态的兜底清理）。"""
        try:
            async with get_session_factory()() as session:
                service = ApprovalService(session, pipeline=self._pipeline)
                await service.close_pending_for_run(run_id, feedback=feedback)
        except Exception as exc:  # noqa: BLE001 —— 兜底清理失败只记日志
            BusinessLog(
                message="兜底关闭 pending 审批失败",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).warning(exc)

    async def _record_segment_model_calls(
        self,
        launch: RunLaunch,
        outcome: RunOutcome | None,
        sink: _RunEventSink,
    ) -> None:
        """逐条落本段 model_calls（+ 残差对账行；outcome=None 仅逐条落）。

        幂等：同一 sink 只落一次。停车路径先记本段（事件可见前成本行必须落库），
        若随后失败转 error 收口，`_finalize` 会对同一 sink 再调一次——不设闸会把每行
        重复计一次（token 双计）。
        """
        if sink.model_calls_recorded:
            return
        sink.mark_model_calls_recorded()
        records = sink.model_calls
        recorded_input = sum(record.input_tokens for record in records)
        recorded_output = sum(record.output_tokens for record in records)
        for record in records:  # 逐行独立 suppress（单行失败不得吞掉后续行）
            await self._record_model_call(self._model_call_entry(launch, record))

        if outcome is None:
            return

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

    async def _prior_model_usage(self, run_id: str) -> Usage:
        """该 run 既往 model_calls 的 token 合计（跨段 usage_totals 的「既往」部分）。

        run 行不存在/查询失败一律按 0——终态事件不该因统计查询失败而丢。
        """
        try:
            key = uuid.UUID(run_id)
        except ValueError:  # pragma: no cover —— run_id 恒来自 DB 行
            return Usage()
        try:
            async with get_session_factory()() as session:
                row = await session.execute(
                    select(
                        func.coalesce(func.sum(ModelCall.input_tokens), 0),
                        func.coalesce(func.sum(ModelCall.output_tokens), 0),
                    ).where(ModelCall.run_id == key)
                )
                input_tokens, output_tokens = row.one()
        except Exception as exc:  # noqa: BLE001 —— 统计失败按 0 记（不影响收口）
            BusinessLog(
                message="既往 model_calls 合计查询失败，usage_totals 按本段计",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).warning(exc)
            return Usage()
        return Usage(input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0))

    async def _set_run_waiting(self, run_id: str) -> bool:
        """把 run 行迁到 waiting_approval 并 commit；返回是否由本次调用完成迁移。

        **条件迁移**（`status = 'running'` → `waiting_approval`，不写 finished_at：
        挂起不是终态，D4）：只把仍在跑的行置为挂起态。返回 False = 行已随会话删除，
        或已被并发收口（stop/error）抢到终态——调用方必须放弃本次挂起，不得发
        `approval_request`（事件可见 ⇒ run 必须可续跑，这是决策端守卫能生效的前提）。

        落库失败**向上抛**（不吞）：挂起态没写成时审批卡片一张都不该发，让 `_finalize`
        按 error 收口（fail-closed），绝不留「事件已发、run 永远 running」的夹生态。
        """
        key = uuid.UUID(run_id)
        try:
            async with get_session_factory()() as session:
                migrated = await session.scalar(
                    update(Run)
                    .where(Run.id == key, Run.status == "running")
                    .values(status="waiting_approval")
                    .returning(Run.id)
                )
                await session.commit()
                if migrated is not None:
                    return True
                status = await session.scalar(select(Run.status).where(Run.id == key))
        except Exception as exc:  # noqa: BLE001 —— 记日志后重抛，由 _finalize 收为 error
            BusinessLog(
                message="run 行挂起态落库失败",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).error(exc)
            raise
        if status is None:
            BusinessLog(
                message="run 行已不存在（会话被删除），跳过审批挂起",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).warning()
        else:
            BusinessLog(
                message="run 行已不在运行态（并发收口抢先），跳过审批挂起",
                action="收口运行",
                entity="run",
                entity_id=run_id,
            ).warning()
        return False

    async def _run_still_waiting(self, run_id: str) -> bool:
        """复核 run 行仍处 waiting_approval（stop 落在两次提交之间的兜底检查）。"""
        async with get_session_factory()() as session:
            status = await session.scalar(select(Run.status).where(Run.id == uuid.UUID(run_id)))
        return status == "waiting_approval"

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
    "ApprovalResumeRegistry",
    "ResumeContext",
    "RunHandle",
    "RunService",
    "RunView",
]
