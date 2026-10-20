"""RunExecutor：外层图的 astream 消费循环（A3 §5.5 职责链的编排层一半）。

职责切分（本车道 vs 流式接口车道）：

- **本模块**：装配内层 agent + 构图 → `astream(stream_mode=[messages, custom,
  updates], subgraphs=True)` 消费 → 原始事件注入 `EventSink` → 异常分类 →
  stop/cancel 收敛 → 终态（`RunOutcome`，含 run 级 usage——run_service 据此记
  model_calls，AC-015-1；F4 起不再设 on_usage 回调）。
- **services/run_service.py（流式接口车道）**：把 EventSink 适配到
  translator+emitter（原始三元组 → 契约域事件 → seq/落库/广播），并用
  `RunOutcome` 收口 run 行与 `translator.finish_run(...)`（在飞消息补
  message_finished(cancelled/error)——translator 已备好该收口）。
- **单一 astream 消费者**（A3 §6.5 强制点 4）：`agent/runtime.py` 之外禁止任何
  模块 `graph.astream`。

中断预留（M3）：updates 通道出现 `__interrupt__`（HITL 挂起）时本模块只置位
`interrupt_pending` 并继续消费到流自然挂起——审批行落库、`approval_request`
事件、`Command(resume=...)` 恢复全部在 M3 审批车道接线（M2 未装 HITL，
该分支实际不可达，保留为契约预留）。

密钥/异常纪律：LLM/SDK 异常一律经 `classify_run_error` 收敛为契约 RunErrorObj
（message 不含 SDK 原文，NFR-004）；原始异常文本不进本模块的任何返回值。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agent.assembly import ModelFactory, build_inner_agent, default_model_factory
from app.agent.graph import build_graph
from app.agent.models_registry import SessionModelCfg
from app.agent.state import SessionCfg
from app.core.config import agent_settings
from app.utils.error_classifier import classify_run_error
from lcode_contracts.common import Usage
from lcode_contracts.errors import RunErrorObj

OutcomeStatus = Literal["finished", "stopped", "error"]
"""run 终态（run_status 的 finished/stopped/error 相位；waiting_approval 是 M3 预留）。"""

StreamMode = Literal["messages", "custom", "updates"]
"""astream 消费的三通道（A3 §6.2 映射表的源面；§6.5 强制点 4 的消费范围）。"""

INTERRUPT_KEY = "__interrupt__"
"""updates 通道的中断信标（langgraph 1.2.16 口径；M3 审批车道消费）。"""


class EventSink(Protocol):
    """原始流事件出口（A3 §5.5：translator+emitter 接线在 run_service）。

    `raw` 即 langgraph `astream` 产出的三元组 `(namespace, mode, payload)`
    （subgraphs=True 形态；主图层事件 namespace 为空元组）——与
    `events/translator.py::_parse_raw` 的入参形态一致。
    """

    async def on_raw_event(self, raw: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """一次 run 的终态（run_service 据此收口 run 行 / translator.finish_run）。"""

    status: OutcomeStatus
    usage: Usage
    error: RunErrorObj | None
    duration_ms: int


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
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.user_id = user_id
        self.sink = sink
        self.run_timeout_s = (
            agent_settings.run_timeout_s if run_timeout_s is None else run_timeout_s
        )
        self.interrupt_pending = False
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[RunOutcome] | None = None
        self._session_cfg: SessionModelCfg | SessionCfg | None = None
        # usage 的单消息窗口（F3）：窗口内分项取最大，窗口关闭折叠进 run 级 totals
        self._usage_msg_id: str | None = None
        self._usage_msg_input = 0
        self._usage_msg_output = 0

    # ------------------------------------------------------------------ 启动面

    def start(
        self,
        *,
        input_messages: list[BaseMessage],
        session_cfg: SessionModelCfg | SessionCfg | None,
        backend: Any,
        checkpointer: BaseCheckpointSaver[Any],
        model_factory: ModelFactory | None = None,
    ) -> asyncio.Task[RunOutcome]:
        """起后台消费任务（run_service 的启动形态；await task 取终态）。"""
        self._task = asyncio.create_task(
            self.run(
                input_messages=input_messages,
                session_cfg=session_cfg,
                backend=backend,
                checkpointer=checkpointer,
                model_factory=model_factory,
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
    ) -> RunOutcome:
        """消费整条 run：装配 → astream → 终态（§5.5 职责链 1-5）。"""
        self._session_cfg = session_cfg
        factory = model_factory if model_factory is not None else default_model_factory(session_cfg)  # type: ignore[arg-type]
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
                    self._input_state(input_messages),
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
            # M3 预留：审批挂起不是终态；届时 run_service 置 run=waiting_approval，
            # 决策后以 Command(resume=...) 复用本 executor 的 sink 形态续跑
            return self._outcome("finished", usage, None, started)
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
        )
        return build_graph(checkpointer, inner_agent=inner)

    def _input_state(self, input_messages: list[BaseMessage]) -> dict[str, Any]:
        return {
            "messages": list(input_messages),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "user_id": self.user_id,
            "session_cfg": self._snapshot(),
        }

    def _snapshot(self) -> dict[str, Any]:
        """会话快照的 state 形态（SessionModelCfg DTO / SessionCfg dict → JSON dict）。"""
        cfg = self._session_cfg
        if cfg is None:
            return {}
        if isinstance(cfg, dict):
            return dict(cfg)
        return {"model": cfg.model, "reasoning_effort": cfg.reasoning_effort}

    def _config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": f"{self.user_id}:{self.session_id}"}}

    def _observe(self, raw: Any, usage: Usage) -> None:
        """消费侧旁路观察：usage 累计（messages 通道）与中断信标（updates 通道）。

        usage 口径（F3，translator 事件侧同构）：**单条消息窗口内分项取最大**（兼容
        「仅末块给总量」与「每块给累计量」两种网关方言），窗口关闭（chunk_position
        =="last" 或消息 id 变化）时折叠进 run 级 totals——跨消息求和；run 结束由
        `_outcome` 兜底折叠残窗。推理 token 已含在 usage_metadata.output_tokens
        （硬约束 9）。本处是 run 级粗汇总（model_calls 与终态回传的唯一口径源），
        translator 另有同口径的事件侧累计，双轨互不干扰。
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
        elif mode == "updates" and isinstance(payload, dict) and INTERRUPT_KEY in payload:
            # M3 预留：HITL 挂起信标（审批行落库/事件/恢复在审批车道接线）
            self.interrupt_pending = True

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


__all__ = ["EventSink", "INTERRUPT_KEY", "OutcomeStatus", "RunExecutor", "RunOutcome"]
