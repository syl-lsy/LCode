"""RunExecutor：外层图的 astream 消费循环（A3 §5.5 职责链的编排层一半）。

职责切分（本车道 vs 流式接口车道）：

- **本模块**：装配内层 agent + 构图 → `astream(stream_mode=[messages, custom,
  updates], subgraphs=True)` 消费 → 原始事件注入 `EventSink` → 异常分类 →
  stop/cancel 收敛 → 段终态（`RunOutcome`，含本段 usage——**仅作运行诊断**；成本
  记账与终态 `usage_totals` 的唯一权威是 translator 的逐次记账，AC-015-1 / M4 前置 D5）。
- **services/run_service.py（流式接口车道）**：把 EventSink 适配到
  translator+emitter（原始三元组 → 契约域事件 → seq/落库/广播），并用
  `RunOutcome` 收口 run 行与 `translator.finish_run(...)`（在飞消息补
  message_finished(cancelled/error)——translator 已备好该收口）。
- **单一 astream 消费者**（A3 §6.5 强制点 4）：`agent/runtime.py` 之外禁止任何
  模块 `graph.astream`。

审批中断（M3，m3-spikes §1.3）：updates 通道出现 `__interrupt__`（HITL 挂起）时——
- `parse_interrupts` 解析出 `(namespace, Interrupt)`；同一 interrupt 会在根图 `()`
  与子图 `('tools:<task_id>',)` 两层各出现一次，本模块按 interrupt id/value 去重并
  只保留**根层**（审批映射的唯一入口）；
- 段末 `interrupt_pending` 为真 → `RunOutcome.status="waiting_approval"`（**不是**
  finished：不调 sink.finish、不发 run_status(finished)，审批行/事件/恢复由服务层接线）；
- resume：同 thread_id 新建 executor，`resume_value` 非空时输入换成
  `langgraph.types.Command(resume=...)`（其余 config 不变，父 thread 上顶层 resume）。

密钥/异常纪律：LLM/SDK 异常一律经 `classify_run_error` 收敛为契约 RunErrorObj
（message 不含 SDK 原文，NFR-004）；原始异常文本不进本模块的任何返回值。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt

from app.agent.assembly import ModelFactory, build_inner_agent, default_model_factory
from app.agent.auto_approval import AutoApprovalAuditSink, AutoApprovalRuntime
from app.agent.graph import build_graph
from app.agent.models_registry import SessionModelCfg
from app.agent.state import SessionCfg
from app.core.config import agent_settings
from app.utils.error_classifier import classify_run_error
from lcode_contracts.common import Usage
from lcode_contracts.errors import RunErrorObj

OutcomeStatus = Literal["finished", "waiting_approval", "stopped", "error"]
"""段终态（run_status 的 finished/stopped/error 相位 + 审批挂起）。

`waiting_approval` 不是 run 的终态：run 行置 waiting_approval，决策后以
`Command(resume=...)` 续跑（同 run_id/thread_id），最终段才收口 finished。
"""

StreamMode = Literal["messages", "custom", "updates"]
"""astream 消费的三通道（A3 §6.2 映射表的源面；§6.5 强制点 4 的消费范围）。"""

INTERRUPT_KEY = "__interrupt__"
"""updates 通道的中断信标（langgraph 1.2.16 口径；M3 审批车道消费）。"""

InterruptEntry = tuple[tuple[str, ...], Interrupt]
"""中断条目：`(namespace, Interrupt)`（namespace 为 astream 元组形态）。"""


class EventSink(Protocol):
    """原始流事件出口（A3 §5.5：translator+emitter 接线在 run_service）。

    `raw` 即 langgraph `astream` 产出的三元组 `(namespace, mode, payload)`
    （subgraphs=True 形态；主图层事件 namespace 为空元组）——与
    `events/translator.py::_parse_raw` 的入参形态一致。
    """

    async def on_raw_event(self, raw: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """一段 run 的终态（run_service 据此收口 run 行 / translator.finish_run）。"""

    status: OutcomeStatus
    usage: Usage
    error: RunErrorObj | None
    duration_ms: int


def parse_interrupts(raw: Any) -> list[InterruptEntry]:
    """一条原始 astream 事件 → 其中的中断条目（未去重；调用方按 id/value 收敛）。

    形态：updates 通道的 `{..., "__interrupt__": (Interrupt, ...)}`（安装版 langgraph
    1.2.16；`Interrupt` 只有 `value`/`id`，命名空间在 astream 三元组的首元素里）。
    其余 mode/载荷形态返回空列表（框架杂音不致命）。
    """
    if not isinstance(raw, (tuple, list)):
        return []
    if len(raw) == 3 and isinstance(raw[1], str):
        namespace = tuple(raw[0]) if isinstance(raw[0], (tuple, list)) else ()
        mode, payload = str(raw[1]), raw[2]
    elif len(raw) == 2 and isinstance(raw[0], str):
        namespace, mode, payload = (), str(raw[0]), raw[1]
    else:
        return []
    if mode != "updates" or not isinstance(payload, Mapping) or INTERRUPT_KEY not in payload:
        return []
    items = payload[INTERRUPT_KEY]
    if isinstance(items, Interrupt):
        items = (items,)
    if not isinstance(items, (list, tuple)):
        return []
    return [(namespace, item) for item in items if isinstance(item, Interrupt)]


def _interrupt_key(interrupt: Interrupt) -> str:
    """中断去重键：优先 interrupt id；缺失时退回 value 的规范化 JSON。

    同一 interrupt 在根图与子图两层各出现一次（spike §1.3），两层的 `id` 相同；
    value 兜底只在极端网关方言（无 id）下生效——仍保证"同一审批只映射一次"。
    """
    ident = getattr(interrupt, "id", None)
    if isinstance(ident, str) and ident:
        return f"id:{ident}"
    try:
        canonical = json.dumps(interrupt.value, sort_keys=True, default=str)
    except TypeError, ValueError:  # pragma: no cover —— value 由框架构造，正常可序列化
        canonical = repr(interrupt.value)
    return f"value:{canonical}"


class RunExecutor:
    """一次 run 的执行器：装配 → 消费 → 收敛（每次 run 新建实例）。"""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        sink: EventSink,
        user_id: str = "local",
        run_timeout_s: int | None = None,
        approval_mode: str = "manual",
        sandbox_provider: str | None = None,
        auto_audit: AutoApprovalAuditSink | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.user_id = user_id
        self.sink = sink
        self.run_timeout_s = (
            agent_settings.run_timeout_s if run_timeout_s is None else run_timeout_s
        )
        self.approval_mode = approval_mode
        """run 启动快照的审批模式（resume 段固定同值，避免决策窗口内翻转，m3-plan D5）。"""
        self.sandbox_provider = sandbox_provider
        self.auto_audit = auto_audit
        """AUTO 分类器的审计落行回调（run_service 以短事务实现；MANUAL 下不用）。"""
        self.interrupt_pending = False
        self._interrupts: dict[str, InterruptEntry] = {}  # 去重后的中断（根层优先）
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[RunOutcome] | None = None
        self._session_cfg: SessionModelCfg | SessionCfg | None = None
        # usage 的单消息窗口（F3）：窗口内分项取最大，窗口关闭折叠进段级 totals
        self._usage_msg_id: str | None = None
        self._usage_msg_input = 0
        self._usage_msg_output = 0

    @property
    def interrupts(self) -> tuple[InterruptEntry, ...]:
        """本段收到的中断（根层 `()`，按 id/value 去重）。

        子图重复条目只保留根层：审批映射以根层载荷为唯一入口（spike §1.3）；
        没有根层条目的中断**不会**被上浮，调用方必须 fail-closed（见 run_service）。
        """
        return tuple(entry for entry in self._interrupts.values() if entry[0] == ())

    # ------------------------------------------------------------------ 启动面

    def start(
        self,
        *,
        input_messages: list[BaseMessage],
        session_cfg: SessionModelCfg | SessionCfg | None,
        backend: Any,
        checkpointer: BaseCheckpointSaver[Any],
        model_factory: ModelFactory | None = None,
        resume_value: Any | None = None,
    ) -> asyncio.Task[RunOutcome]:
        """起后台消费任务（run_service 的启动形态；await task 取段终态）。

        `resume_value` 非 None = 审批决策后的续跑（输入换 `Command(resume=...)`，
        同 thread_id；见 run()）。
        """
        self._task = asyncio.create_task(
            self.run(
                input_messages=input_messages,
                session_cfg=session_cfg,
                backend=backend,
                checkpointer=checkpointer,
                model_factory=model_factory,
                resume_value=resume_value,
            )
        )
        return self._task

    def request_stop(self) -> None:
        """协作式停止：置位后在下一事件边界退出；in-flight await 由任务取消解锁。"""
        self._stop_requested.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------ 主链路

    async def run(
        self,
        *,
        input_messages: list[BaseMessage],
        session_cfg: SessionModelCfg | SessionCfg | None,
        backend: Any,
        checkpointer: BaseCheckpointSaver[Any],
        model_factory: ModelFactory | None = None,
        resume_value: Any | None = None,
    ) -> RunOutcome:
        """消费一段 run：装配 → astream → 段终态（§5.5 职责链 1-5）。

        `resume_value`（`{"decisions": [...]}`）非 None 时输入为
        `Command(resume=...)`——resume 段只重跑被 interrupt 挂起的节点，不重放 entry
        （所以不会重复 run_status(started)）。
        """
        self._session_cfg = session_cfg
        factory = model_factory if model_factory is not None else default_model_factory(session_cfg)
        graph = self._build(
            session_cfg=session_cfg, backend=backend, checkpointer=checkpointer, factory=factory
        )
        usage = Usage()
        started = time.monotonic()

        try:
            async with asyncio.timeout(self.run_timeout_s):
                # list[StreamMode]（而非 tuple 字面量）：mypy 对 astream 重载的
                # Sequence[Literal[...]] 只接受 list 形态（本仓 mypy 实测）
                stream_modes: list[StreamMode] = ["messages", "custom", "updates"]
                async for raw in graph.astream(
                    self._input_state(input_messages, resume_value=resume_value),
                    config=self._config(),
                    stream_mode=stream_modes,
                    subgraphs=True,
                ):
                    await self.sink.on_raw_event(raw)
                    self._observe(raw, usage)
                    if self._stop_requested.is_set():
                        break
        except asyncio.CancelledError:
            # 外部任务取消 / request_stop 的解锁路径：AC-001-2——已生成内容保留
            # （沿途已过 sink），终态 stopped，不向调用方再抛 CancelledError
            # （后台任务收敛为正常返回值）。
            return self._outcome("stopped", usage, None, started)
        except TimeoutError:
            error = classify_run_error(TimeoutError(), message="run 超过墙钟上限被终止")
            return self._outcome("error", usage, error, started)
        except Exception as exc:
            error = classify_run_error(exc)
            return self._outcome("error", usage, error, started)

        if self._stop_requested.is_set():
            return self._outcome("stopped", usage, None, started)
        if self.interrupt_pending:
            # 审批挂起不是终态（m3-plan D4）：段结束但不收口 finished；审批行/事件/
            # resume 由 run_service 接线，最终段才发 run_status(finished)。
            return self._outcome("waiting_approval", usage, None, started)
        return self._outcome("finished", usage, None, started)

    # ------------------------------------------------------------------ 内部

    def _build(
        self,
        *,
        session_cfg: SessionModelCfg | SessionCfg | None,
        backend: Any,
        checkpointer: BaseCheckpointSaver[Any],
        factory: ModelFactory,
    ) -> CompiledStateGraph:
        inner = build_inner_agent(
            session_cfg,
            backend,
            model_factory=factory,
            approval_mode=self.approval_mode,
            auto_approval=AutoApprovalRuntime(
                session_id=self.session_id,
                run_id=self.run_id,
                user_id=self.user_id,
                audit=self.auto_audit,
            ),
        )
        return build_graph(checkpointer, inner_agent=inner)

    def _input_state(
        self, input_messages: list[BaseMessage], *, resume_value: Any | None = None
    ) -> Any:
        """首段 = 新消息状态；resume 段 = `Command(resume=...)`（同 thread 续跑）。"""
        if resume_value is not None:
            return Command(resume=resume_value)
        return {
            "messages": list(input_messages),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "user_id": self.user_id,
            "session_cfg": self._snapshot(),
        }

    def _snapshot(self) -> dict[str, Any]:
        """会话快照的 state 形态（SessionModelCfg DTO / SessionCfg dict → JSON dict）。

        `approval_mode` / `sandbox_provider` 由 run 启动快照注入（run_service 从会话行
        读出）；DTO 形态缺这两个旋钮时用 executor 快照兜底——checkpoint 里的快照必须
        完整（审批策略在 resume 段据此复现）。
        """
        cfg = self._session_cfg
        if cfg is None:
            snapshot: dict[str, Any] = {}
        elif isinstance(cfg, dict):
            snapshot = dict(cfg)
        else:
            snapshot = {"model": cfg.model, "reasoning_effort": cfg.reasoning_effort}
        snapshot.setdefault("approval_mode", self.approval_mode)
        if self.sandbox_provider is not None:
            snapshot.setdefault("sandbox_provider", self.sandbox_provider)
        return snapshot

    def _config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": f"{self.user_id}:{self.session_id}"}}

    def _observe(self, raw: Any, usage: Usage) -> None:
        """消费侧旁路观察：usage 累计（messages 通道）与中断信标（updates 通道）。

        usage 口径（F3，translator 事件侧同构）：**单条消息窗口内分项取最大**（兼容
        「仅末块给总量」与「每块给累计量」两种网关方言），窗口关闭（chunk_position
        =="last" 或消息 id 变化）时折叠进段级 totals——跨消息求和；段结束由
        `_outcome` 兜底折叠残窗。推理 token 已含在 usage_metadata.output_tokens
        （硬约束 9）。**本处只作运行诊断**（失败排查/超时定位）：单窗口在并行子智能体
        交错流式时会把同一消息的累计量重复折叠，故不参与成本记账——成本行与终态
        `usage_totals` 以 translator 的 per-agent 逐次记账为唯一权威（M4 前置 D5）。

        中断：`parse_interrupts` 解析 `__interrupt__`，按 id/value 去重且**根层优先**
        （同一 interrupt 在根图/子图各出现一次——审批映射只认根层，spike §1.3）。
        """
        parts = raw if isinstance(raw, tuple) else ()
        if len(parts) == 3:  # (namespace, mode, payload)——subgraphs=True 形态
            _namespace, mode, payload = parts
        elif len(parts) == 2:  # (mode, payload)
            mode, payload = parts
        else:
            return

        if mode == "messages" and isinstance(payload, tuple) and payload:
            self._absorb_message_usage(payload[0], usage)
        elif mode == "updates":
            for namespace, interrupt in parse_interrupts(raw):
                self._record_interrupt(namespace, interrupt)

    def _record_interrupt(self, namespace: tuple[str, ...], interrupt: Interrupt) -> None:
        """中断建账：置挂起信标 + 去重收集（根层 `()` 覆盖子图条目，反之不覆盖）。"""
        self.interrupt_pending = True
        key = _interrupt_key(interrupt)
        existing = self._interrupts.get(key)
        if existing is None or (existing[0] != () and namespace == ()):
            self._interrupts[key] = (namespace, interrupt)

    def _absorb_message_usage(self, chunk: Any, usage: Usage) -> None:
        """单消息窗口吸收 usage_metadata：分项取最大；窗口边界折叠（消息 id 变化/末块）。"""
        meta = getattr(chunk, "usage_metadata", None)
        if isinstance(meta, dict):
            message_id = getattr(chunk, "id", None)
            if message_id and self._usage_msg_id is not None and message_id != self._usage_msg_id:
                self._flush_message_usage(usage)
            self._usage_msg_id = message_id or self._usage_msg_id
            self._usage_msg_input = max(self._usage_msg_input, int(meta.get("input_tokens") or 0))
            self._usage_msg_output = max(
                self._usage_msg_output, int(meta.get("output_tokens") or 0)
            )
        if getattr(chunk, "chunk_position", None) == "last":
            self._flush_message_usage(usage)

    def _flush_message_usage(self, usage: Usage) -> None:
        """当前消息窗口折叠进 run 级 totals 并复位（跨消息求和的落点）。"""
        usage.input_tokens += self._usage_msg_input
        usage.output_tokens += self._usage_msg_output
        self._usage_msg_input = 0
        self._usage_msg_output = 0
        self._usage_msg_id = None

    def _outcome(
        self,
        status: OutcomeStatus,
        usage: Usage,
        error: RunErrorObj | None,
        started: float,
    ) -> RunOutcome:
        self._flush_message_usage(usage)  # 残窗兜底折叠（末块无 "last" 标记的方言）
        return RunOutcome(
            status=status,
            usage=usage,
            error=error,
            duration_ms=max(int((time.monotonic() - started) * 1000), 0),
        )


__all__ = [
    "EventSink",
    "INTERRUPT_KEY",
    "InterruptEntry",
    "OutcomeStatus",
    "RunExecutor",
    "RunOutcome",
    "parse_interrupts",
]
