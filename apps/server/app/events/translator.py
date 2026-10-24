"""事件翻译器：原始 LangGraph astream 事件 → 契约域事件（A3 §6.2 映射表；REQ-008）。

**不透传原始事件**（AC-008-2）：本模块是 astream 原始流的唯一解释者——产出物一律是
`lcode_contracts.events` 注册表内的契约事件（seq 先置 0 占位，由 `emitter.EventPipeline`
统一分配真实 seq；`created_at` 在此打点，重放与实时逐字节一致）。

原始流形态（安装版 langgraph 1.2.16 实测，U2 spike 固化）：
- `graph.astream(..., stream_mode=[...])` 产出 `(mode, payload)`；
- 配 `subgraphs=True` 时产出 `(namespace_tuple, mode, payload)`，两种形态本模块都收；
- `mode="messages"`：`payload = (message_chunk, metadata)`——文本/思考增量、usage、收口信号；
- `mode="updates"`：`payload = {node: state_update}`——聚合 AIMessage（完整 tool_calls）与
  ToolMessage（工具结果）；
- `mode="custom"`：`get_stream_writer()` 写入的自定义载荷——本模块定义两个协议键：
  * `{"type": "<契约事件 type>", ...}`：透传（补信封后校验；中间件由此发自定义域事件，
    如压缩提示 `run_status(subject=compact, ...)`）；工具族事件在此通道同样过
    messages/updates 的去重锚（_tool_calls/_finished_tool_calls，F2）；
  * `{"structured_result": {...}}`：附着到下一条收口的 assistant 消息
    （reviewer findings → `message_finished.structured`，Q6/Q2 定稿）。

U2「AgentRef 推导」实测口径（回归锚 = `test/unit/test_event_translator.py` 的 golden）：
- metadata 键：`langgraph_checkpoint_ns` / `checkpoint_ns`，嵌套形态 `parent:uuid|child:uuid`
  （多段 = 子图/子智能体作用域）；
- task() 调用栈：`tool_call_started(tool="task")` 入栈（args.subagent_type = 角色）→
  栈内作用域的事件挂 `spawned_by_tool_call_id` → 对应 ToolMessage 收口出栈；
- role 兜底：命名空间段名 / `langgraph_node` 命中 `subagent_roles`（无 task 栈的直嵌子图）。

已知的取舍（都写在对应分支的注释里）：
- usage 按「分项最大值」吸收（兼容「仅末块给总量」与「每块给累计量」两种网关方言），
  跨消息求和；推理 token 已含在 `usage_metadata.output_tokens`（硬约束 9 的网关口径），
  不再叠加 `output_token_details.reasoning`，避免双计；
- **模型调用逐次记账**（AC-015-1）：每收口一条 assistant 消息（= 一次模型调用）就在
  `model_calls` 追加一条 `ModelCallRecord`——包括无 usage 的 0 token 调用与强制收口的
  在飞消息；记账点唯一（`_close_message`），与消息回声去重锚共用同一收口时机，故不会
  把同一次调用记成多行；
- exit_code / 截断 / offload 引用：优先 `ToolMessage.artifact`，回退解析 deepagents
  0.6.12 的正文标记（`middleware/filesystem.py` 实测三种标记，见下方正则）；
- tap 中间件（LCodeEventTapMiddleware）经 custom 通道先发的 `tool_call_finished`
  只带计时/错误分类（安装版 deepagents 的 execute 不写 artifact）：工具调用仍在飞
  （ToolMessage 未到）时寄存为附言，由 updates 通道的 ToolMessage 收口合并进唯一的
  rich finished（exit_code/result_preview/truncated 只在该通道）——custom 先认领会
  把 rich 收口整体吞掉（P0 实测：接 tap 后每个 finished 永远没有 exit_code 与输出
  预览）；ToolMessage 永不到（异常逃出工具节点 / stop 打断）由 `finish_run` 冲刷；
- 消息回声去重两道锚均取**全局口径**（拦得住跨作用域回声）：上游 id 锚拦主图对
  execute 子图消息的状态回声（namespace=() → agent_key 不同，模型侧 id 全局唯一），
  防「同文气泡双发 + usage 双计」；粘性「已流式」标志兜无 id 的同源聚合尾巴，跨过
  工具回合（ToolMessage 收口）即清零——非流式模型的下一轮终答是新调用，不得误伤；
- P0 不产 `browser_action`（本模块压根不构造它；出口另有 `P0_EVENT_TYPES` 硬守卫）；
- 审批族（M3）：`translate()` 对 `__interrupt__` 载荷仍不产事件（approval_request 的
  `approval_id` 由服务层在审批行落库后传入，见 `approval_request_event`）；**映射**
  走独立入口 `describe_interrupt()`——把 `HITLRequest.action_requests` 按 `(name,args)`
  与在飞 tool calls 的尾部切片对位成 `ApprovalTarget`。对位失败 **fail-closed**
  （抛 `InterruptMappingError` → run 以 error 收敛，绝不自动放行）；
- resume 段去重：`resumed_tool_calls`（富快照：id/tool/args/agent_ref）或
  `resumed_tool_call_ids`（仅 id）播种 `_tool_calls`——resume 时被挂起的工具调用会
  重放 started，`task` 调用还要靠富快照重建 task 栈（子智能体事件归属）；
  `resumed_closed_tool_call_ids` 播种 `_finished_tool_calls`——审批恢复会让图从
  checkpoint 续跑并把**整段历史**的 AIMessage/ToolMessage 重发一遍，只播种在飞调用
  不够：历史里已完成的调用会二次公告 started 与 finished（同一 run 内重复工具卡）；
  `resumed_message_ids` 播种 `_closed_upstream_ids`——after_model 与 execute 节点的
  最终状态更新都会把已处理过的 AIMessage 重发，不播种会重复 message_finished 与
  模型调用记账（token 双计）。
"""

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage

from app.utils.error_classifier import classify_run_error, classify_tool_error, classify_tool_text
from lcode_contracts.common import AgentRef, TodoItem, Usage
from lcode_contracts.errors import RunErrorObj, ToolErrorObj
from lcode_contracts.events import (
    EVENT_TYPES,
    ApprovalRequestEvent,
    BaseEvent,
    MessageDeltaEvent,
    MessageFinishedEvent,
    RunStatusEvent,
    StructuredResult,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)

# ---------------------------------------- 协议常量（对齐 deepagents 0.6.12 与中间件车道）

PROTOCOL_TASK_TOOL = "task"
"""deepagents SubAgentMiddleware 的子智能体派发工具名（subagents.py 实测）。"""

PROTOCOL_WRITE_TODOS_TOOL = "write_todos"
"""langchain TodoListMiddleware 的 todo 工具名；面板数据源 = 其 finished.todos 快照。"""

DEFAULT_SUBAGENT_ROLES = frozenset({"general-purpose", "explorer", "coder", "reviewer"})
"""P0 预置子智能体角色（A3 §5.3）；命名空间段名/节点名命中其一即可兜底定 role。"""

DEFAULT_IDEMPOTENT_TOOLS = frozenset({"read_file", "ls", "glob", "grep"})
"""幂等白名单（ADR-002 ToolRetryMiddleware 同源口径）：错误 retryable 建议仅对这些工具给 True。"""

DEFAULT_RESULT_PREVIEW_LIMIT = 2000
"""result_preview 截断长度：超出置 `truncated=True`（完整结果本就在 ToolMessage/沙箱里）。"""

CUSTOM_STRUCTURED_KEY = "structured_result"
"""custom 通道协议键：reviewer 结构化结果附着（见模块 docstring 的 custom 协议）。"""

_ENVELOPE_FIELDS: tuple[str, ...] = ("seq", "session_id", "run_id", "agent", "created_at")
"""信封字段（透传 custom 载荷时剥掉，translator 统一补）。"""

_EXIT_CODE_MARKER = re.compile(r"\[Command (?:succeeded|failed) with exit code (-?\d+)\]")
"""deepagents 0.6.12 execute 的退出码正文标记（middleware/filesystem.py:1752-1754 实测）。"""

_TRUNCATION_MARKER = "[Output was truncated due to size limits]"
"""deepagents 输出截断标记（filesystem.py:1756-1757 实测）。"""

_OFFLOAD_PATH_MARKER = re.compile(r"saved in the filesystem at this path:\s*(\S+)")
"""deepagents 大结果 offload 落盘路径标记（_message_eviction.py TOO_LARGE_TOOL_MSG 实测）。"""

_CHECKPOINT_NS_KEYS: tuple[str, ...] = ("langgraph_checkpoint_ns", "checkpoint_ns")
"""子图命名空间键（安装版实测两者都有，前者是 langgraph 标准键）。"""

_TODO_STATUSES = ("pending", "in_progress", "completed")

FinishReason = Literal["stop", "length", "content_filter", "error", "cancelled"]
RiskLevel = Literal["low", "medium", "high"]

RiskLevelResolver = Callable[[str, Mapping[str, Any]], RiskLevel | None]
"""风险等级解析器：由运行时注入（查 agent/permissions 规则表；本包禁止 import agent/）。"""

ApprovalReasonResolver = Callable[[str, Mapping[str, Any]], str]
"""审批理由解析器：纯函数（agent/permissions.approval_reason），由运行时注入。"""


class InterruptMappingError(ValueError):
    """中断对位失败（fail-closed 信号）。

    审批只能按「在飞 tool calls ↔ action_requests」的确定映射落行；映射不出来时
    必须让 run 以 error 收敛（run_service 捕获后按 INTERNAL 收敛），**绝不**退回
    「全部放行」或「丢弃审批」。
    """


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid4_str() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- 原始事件归一化


@dataclass(frozen=True)
class _RawEvent:
    """归一化后的原始 astream 事件片段。"""

    mode: str
    payload: Any
    namespace: tuple[str, ...] = ()


def _parse_raw(raw: Any) -> _RawEvent | None:
    """`(mode, payload)` / `(namespace, mode, payload)` → 归一化；其余形态忽略。"""
    if isinstance(raw, (tuple, list)):
        if len(raw) == 3 and isinstance(raw[1], str):
            namespace = raw[0] if isinstance(raw[0], (tuple, list)) else ()
            return _RawEvent(mode=str(raw[1]), payload=raw[2], namespace=tuple(namespace))
        if len(raw) == 2 and isinstance(raw[0], str):
            return _RawEvent(mode=str(raw[0]), payload=raw[1])
    return None


# ---------------------------------------------------------------- 内部状态


@dataclass
class _OpenMessage:
    """一条在途 assistant 消息的累积状态（按 agent 作用域隔离）。"""

    message_id: str
    started_at: datetime  # 本次模型调用的起始时刻（记账行 created_at 的来源）
    upstream_id: str | None = None  # 模型侧消息 id（updates 聚合消息据此识别为同一条）
    input_tokens: int = 0
    output_tokens: int = 0
    usage_seen: bool = False
    emitted_content: bool = False  # 已发过增量（聚合消息不得重发整段）
    structured: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    """一次模型调用的记账快照（AC-015-1 逐次口径；run_service 收口时逐条落 model_calls）。

    只承载 translator 侧可观测的事实：消息 id、起止时刻、本次 usage 与归属。
    role 为 `AgentRef.role` 原值（可能为 None/`general-purpose` 等），到 DB 值的映射
    （general-purpose → main、None/未知 → main）归 run_service。
    """

    message_id: str
    started_at: datetime
    finished_at: datetime
    input_tokens: int
    output_tokens: int
    agent_id: str | None
    role: str | None


@dataclass(frozen=True, slots=True)
class ApprovalTarget:
    """一次待审批工具调用的对位结果（translator → run_service → approvals 行/事件）。

    `tool_call_id` 是审批行与后续 resume 决策的唯一锚：approvals 表的
    `tool_call_id` 列、`approval_request` 事件、以及 resume 段 translator 播种
    全部用它；`source_message_id` 是该调用所属 AI 消息的上游 id（resume 段播种
    `_closed_upstream_ids` 用，防 after_model 重发消息导致重复记账）。
    """

    tool_call_id: str
    tool: str
    args: dict[str, Any]
    risk_level: Literal["medium", "high"]
    reason: str | None
    agent_ref: AgentRef | None = None
    source_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class InFlightToolCall:
    """一条在飞工具调用的跨段快照（resume 播种用；见 EventTranslator 构造器）。

    只播 id 会丢掉两件影响正确性的事：`task` 调用需要重建 task 栈（子智能体事件的
    role/agent 归属），以及 updates/tap 双通道的 started 去重需要知道该调用已在飞。
    """

    tool_call_id: str
    tool: str
    args: dict[str, Any]
    agent_ref: AgentRef | None = None
    source_message_id: str | None = None


@dataclass
class _TapFinish:
    """tap 中间件经 custom 通道寄存的收口附言（计时/错误分类权威源，先于 ToolMessage 到）。

    P0 合并口径：附言本身不发事件（custom 通道没有 exit_code/正文/截断），由
    `_on_tool_message` 合并进唯一 rich finished；ToolMessage 永不到时由
    `_flush_open_tool_calls` 按附言冲刷收口。
    """

    duration_ms: int
    status: Literal["success", "error"]
    error: ToolErrorObj | None = None


@dataclass
class _OpenToolCall:
    """一次在途工具调用（started → finished 配对的锚）。"""

    tool_call_id: str
    tool: str
    args: dict[str, Any]
    agent_ref: AgentRef | None
    started_at: datetime | None
    subagent_role: str | None = None
    tap_finish: _TapFinish | None = None  # tap 附言（custom 通道先到的 finished）
    source_message_id: str | None = None  # 携带该 tool_call 的 AI 消息上游 id（resume 播种用）


# ---------------------------------------------------------------- 翻译器


class EventTranslator:
    """有状态翻译器：一次 run 一个实例（run_id 绑定；跨 run 复用请重建）。

    注入点（测试/上层可替换）：`now`（时钟）、`new_id`（消息 id 生成）、
    `risk_level_for`（工具风险等级，运行时接 permissions 规则表）、
    `idempotent_tools`（retryable 建议的幂等白名单）、`subagent_roles`（role 兜底名单）。
    """

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        preview_limit: int = DEFAULT_RESULT_PREVIEW_LIMIT,
        now: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], str] = _uuid4_str,
        risk_level_for: RiskLevelResolver | None = None,
        approval_reason: ApprovalReasonResolver | None = None,
        idempotent_tools: frozenset[str] = DEFAULT_IDEMPOTENT_TOOLS,
        subagent_roles: frozenset[str] = DEFAULT_SUBAGENT_ROLES,
        resumed_tool_call_ids: Iterable[str] = (),
        resumed_tool_calls: Iterable[InFlightToolCall] = (),
        resumed_closed_tool_call_ids: Iterable[str] = (),
        resumed_message_ids: Iterable[str] = (),
    ) -> None:
        self._session_id = session_id
        self._run_id = run_id
        self._preview_limit = preview_limit
        self._now = now
        self._new_id = new_id
        self._risk_level_for = risk_level_for
        self._approval_reason = approval_reason
        self._idempotent_tools = idempotent_tools
        self._subagent_roles = subagent_roles

        self._open_messages: dict[str, _OpenMessage] = {}  # agent_key → 在途消息
        # 回声去重两道锚均取全局口径（跨作用域拦得住，见模块 docstring「消息回声去重」）
        self._closed_streamed = False  # 最近一次收口是否流式发过增量（工具回合边界清零）
        self._closed_upstream_ids: set[str] = set()  # 已收口消息的上游 id（模型侧 id 全局唯一）
        self._tool_calls: dict[str, _OpenToolCall] = {}  # tool_call_id → 在途调用
        self._finished_tool_calls: set[str] = set()  # 已收口调用（双通道去重）
        self._seen_tool_call_ids: set[str] = set()  # 本段出现过的全部调用 id（跨段播种用）
        self._task_stack: list[_OpenToolCall] = []  # 活跃 task() 调用栈（内层在后）
        self._pending_structured: dict[str, Any] | None = None
        self._usage_totals = Usage()
        self._model_calls: list[ModelCallRecord] = []
        self._run_started = False

        # resume 播种（m3-plan Phase 1 F）：审批通过的调用在 resume 段会被重放
        # started（tap + after_model 状态更新两条源），挂起回合的 AIMessage 也会被
        # after_model / execute 节点的最终状态更新重发。播种后两条去重锚天然生效，
        # 且**不影响**本段的新调用。
        #
        # 两种形态：`resumed_tool_calls`（富快照：含 tool/args/agent_ref，可重建 task
        # 栈——子智能体事件归属靠它）优先；`resumed_tool_call_ids`（仅 id）用于轻量
        # 调用方，播种为占位锚（只做 started 去重）。
        for call in resumed_tool_calls:
            self._seed_tool_call(
                _OpenToolCall(
                    tool_call_id=call.tool_call_id,
                    tool=call.tool,
                    args=dict(call.args),
                    agent_ref=call.agent_ref,
                    started_at=None,
                    subagent_role=(
                        str(call.args.get("subagent_type"))
                        if call.tool == PROTOCOL_TASK_TOOL
                        else None
                    ),
                    source_message_id=call.source_message_id,
                )
            )
        for call_id in resumed_tool_call_ids:
            self._seed_tool_call(
                _OpenToolCall(
                    tool_call_id=call_id,
                    tool="",
                    args={},
                    agent_ref=None,
                    started_at=None,
                )
            )
        # 已收口调用的播种（resume 段历史重放去重）：审批挂起会让图从 checkpoint 续跑，
        # execute 节点最终更新会把**整段历史**的 AIMessage/ToolMessage 再发一遍——不只是
        # 挂起批次。只播种在飞调用（上面的两条）时，历史里**已完成**的调用会重发
        # started 与 finished（同一 run 内同一 tool_call_id 出现两条卡片事件）。
        # 播种面同时写入两道去重锚：started 检查 `_finished_tool_calls`，finished 由
        # `_claim_tool_finish` 认领——历史重放两条通道都被拦住。
        for closed_id in resumed_closed_tool_call_ids:
            if not closed_id:
                continue
            self._finished_tool_calls.add(closed_id)
            self._seen_tool_call_ids.add(closed_id)
        self._closed_upstream_ids.update(resumed_message_ids)

    def _seed_tool_call(self, call: _OpenToolCall) -> None:
        """播种一条在飞调用（幂等）；task 调用同时入 task 栈（恢复子智能体归属）。"""
        self._seen_tool_call_ids.add(call.tool_call_id)
        if call.tool_call_id in self._tool_calls:
            return
        self._tool_calls[call.tool_call_id] = call
        if call.tool == PROTOCOL_TASK_TOOL:
            self._task_stack.append(call)

    @property
    def open_tool_calls(self) -> tuple[InFlightToolCall, ...]:
        """当前在飞（未收口）工具调用的快照（park 时写入 ResumeContext，resume 段回流）。"""
        return tuple(
            InFlightToolCall(
                tool_call_id=call.tool_call_id,
                tool=call.tool,
                args=dict(call.args),
                agent_ref=call.agent_ref,
                source_message_id=call.source_message_id,
            )
            for call in self._tool_calls.values()
        )

    @property
    def handled_message_ids(self) -> tuple[str, ...]:
        """已收口消息的上游 id（跨段播种：execute 节点最终更新会重发整段历史）。"""
        return tuple(self._closed_upstream_ids)

    @property
    def closed_tool_call_ids(self) -> tuple[str, ...]:
        """本段**已收口**（不在飞）的工具调用 id（resume 播种：历史重放去重）。

        只取「出现过但已不在飞」的调用；在飞调用由 `open_tool_calls` 的富快照播种
        （还要恢复 task 栈/归属），两者互补且不重叠。
        """
        return tuple(self._seen_tool_call_ids - set(self._tool_calls))

    # ------------------------------------------------------------ 对外入口

    def translate(self, raw: Any) -> list[BaseEvent]:
        """一条原始 astream 事件 → 0..N 条契约域事件（顺序 = 产出顺序）。

        无法识别的 mode / 载荷形态安静忽略（框架杂音不致命）；契约校验失败的透传载荷
        直接抛（构造期校验，出口还有第二道闸门——fail loudly 优于静默吞掉）。
        """
        parsed = _parse_raw(raw)
        if parsed is None:
            return []
        if parsed.mode == "messages":
            return self._translate_messages(parsed)
        if parsed.mode == "updates":
            return self._translate_updates(parsed)
        if parsed.mode == "custom":
            return self._translate_custom(parsed)
        return []

    def start_run(self) -> BaseEvent | None:
        """`run_status(run, started)`；幂等——一次运行只发一次。"""
        if self._run_started:
            return None
        self._run_started = True
        return self._run_status_event("started")

    def finish_run(
        self,
        phase: Literal["finished", "stopped", "error"],
        *,
        error: RunErrorObj | None = None,
        usage_offset: Usage | None = None,
    ) -> list[BaseEvent]:
        """收口：在飞消息补 `message_finished` + `run_status`（AC-001-2 的 stopped/error 路径）。

        - `finished`：`usage_totals` = **本段累计 + `usage_offset`（既往段落合计）**；
          求和必须发生在 `close_open_message` **之后**——在飞消息的 usage 只有收口那
          一刻才从 `_open_messages` 折叠进 `_usage_totals`（M4 前置 D5：事件 / DB 行 /
          残差对账三者同源，调用方不得预读本段用量）；
        - `stopped`/`error`：不带 totals（契约允许），`usage_offset` 忽略；
        - `error`：契约硬校验必须携带 `RunErrorObj`（缺失即抛——给运行时一个明确的失败点）；
        - 在飞消息的 finish_reason：finished→stop / stopped→cancelled / error→error。
        """
        finish_reason_map: dict[str, FinishReason] = {
            "finished": "stop",
            "stopped": "cancelled",
            "error": "error",
        }
        finish_reason = finish_reason_map[phase]
        events = self.close_open_message(finish_reason)
        events.extend(self._flush_open_tool_calls())
        totals: Usage | None = None
        if phase == "finished":
            totals = self._usage_totals
            if usage_offset is not None:
                totals = Usage(
                    input_tokens=totals.input_tokens + usage_offset.input_tokens,
                    output_tokens=totals.output_tokens + usage_offset.output_tokens,
                )
        events.append(self._run_status_event(phase, error=error, usage_totals=totals))
        return events

    def close_open_message(self, finish_reason: FinishReason) -> list[BaseEvent]:
        """收口全部在途 assistant 消息（用户停止/异常时保住已生成内容，AC-001-2）。"""
        events: list[BaseEvent] = []
        for key in list(self._open_messages):
            open_message = self._open_messages.get(key)
            if open_message is None:
                continue
            events.extend(
                self._close_message(key, self._ref_of(key), open_message, finish_reason, force=True)
            )
        return events

    @property
    def usage_totals(self) -> Usage:
        """本段累计用量（Σ 各已收口 assistant 消息；推理 token 已含在 output 口径内）。

        **成本记账的权威口径**（M4 前置 D5）：逐次 `model_calls` 与残差对账都以此为准；
        `RunOutcome.usage`（runtime 侧 messages 通道单窗口）只作运行诊断，不参与记账。
        """
        return self._usage_totals

    @property
    def model_calls(self) -> tuple[ModelCallRecord, ...]:
        """逐次模型调用记账快照（只读；一次调用一条，含 0 token 调用）。"""
        return tuple(self._model_calls)

    # ------------------------------------------------------------ 审批族（M3）

    def describe_interrupt(self, namespace: tuple[str, ...], payload: Any) -> list[ApprovalTarget]:
        """`HITLRequest` 中断 → `ApprovalTarget` 列表（fail-closed 对位，m3-plan D3）。

        对位口径（安装版 langchain 1.4.6 `HumanInTheLoopMiddleware` batched 模式）：
        - 一个 model turn 一个 interrupt；`action_requests` **按序**列出被挂起的工具调用；
        - interrupt 在 after_model 节点抛点，**先于工具执行**——但模型节点的状态更新
          已经提交：translator 已从 updates 通道为这些调用发过 `tool_call_started`
          （spike 实测），因此它们在 `_tool_calls`（在飞集合）里；
        - 按 `(name, args)` 精确对位：在飞集合按入栈序扫描（游标只前进），同名同参
          的重复调用按出现序一一配对；`task` 半开（父图 task 的 ToolMessage 要等
          resume 后才产生）不参与对位也不报错——它不在 action_requests 里。

        `namespace` 非空 = 子图重复条目：返回 []（根层 `()` 是唯一入口，spike §1.3）。
        任何无法确定映射的情形（载荷畸形、数量不符、名称/参数对不上）抛
        `InterruptMappingError`——调用方必须让 run 以 error 收敛，绝不自动放行。
        """
        if namespace:
            return []
        value: Any = getattr(payload, "value", payload)
        action_requests = value.get("action_requests") if isinstance(value, Mapping) else None
        if not isinstance(action_requests, (list, tuple)) or not action_requests:
            raise InterruptMappingError(
                "审批中断载荷缺少 action_requests（无法映射到工具调用，按 fail-closed 终止）"
            )

        candidates = list(self._tool_calls.values())
        targets: list[ApprovalTarget] = []
        matched_ids: set[str] = set()
        cursor = 0
        for entry in action_requests:
            name, args = self._action_request_parts(entry)
            found_index = None
            for index in range(cursor, len(candidates)):
                candidate = candidates[index]
                if candidate.tool_call_id in matched_ids:
                    continue
                if candidate.tool != name:
                    continue
                if dict(candidate.args) != args:
                    continue
                found_index = index
                break
            if found_index is None:
                raise InterruptMappingError(
                    f"审批中断无法对位到在飞工具调用（tool={name}）："
                    "按 fail-closed 终止运行，不自动放行"
                )
            matched_ids.add(candidates[found_index].tool_call_id)
            cursor = found_index + 1
            targets.append(self._approval_target(candidates[found_index], name, args))

        # 同一 tool_call_id 只映射一次（子图/根图重复条目已在命名空间层拦下，这里兜底）
        unique: dict[str, ApprovalTarget] = {}
        for target in targets:
            unique.setdefault(target.tool_call_id, target)
        return list(unique.values())

    def approval_request_event(
        self, approval_id: str, target: ApprovalTarget
    ) -> ApprovalRequestEvent:
        """审批行落库后的事件构造入口（`approval_id` 由 service 传入）。

        事件是审批卡片的实时来源；`risk_level`/`reason` 来自 translator 注入的解析器
        （agent/permissions），未知工具按 medium 兜底（进审批面的调用至少 medium）。
        """
        return ApprovalRequestEvent(
            seq=0,
            session_id=self._session_id,
            run_id=self._run_id,
            agent=target.agent_ref,
            created_at=self._now(),
            approval_id=approval_id,
            tool_call_id=target.tool_call_id,
            tool=target.tool,
            args=target.args,
            risk_level=target.risk_level,
            reason=target.reason,
        )

    def _action_request_parts(self, entry: Any) -> tuple[str, dict[str, Any]]:
        """单条 action_request → (name, args)；结构不合法即 fail-closed。"""
        if not isinstance(entry, Mapping):
            raise InterruptMappingError("审批中断的 action_request 不是对象（fail-closed）")
        name = entry.get("name")
        raw_args = entry.get("args")
        if not isinstance(name, str) or not name:
            raise InterruptMappingError("审批中断的 action_request 缺少工具名（fail-closed）")
        if raw_args is not None and not isinstance(raw_args, Mapping):
            raise InterruptMappingError("审批中断的 action_request 参数不是对象（fail-closed）")
        return name, dict(raw_args or {})

    def _approval_target(
        self, call: _OpenToolCall, name: str, args: dict[str, Any]
    ) -> ApprovalTarget:
        risk = None
        if self._risk_level_for is not None:
            risk = self._risk_level_for(name, args)
        reason = None
        if self._approval_reason is not None:
            reason = self._approval_reason(name, args)
        return ApprovalTarget(
            tool_call_id=call.tool_call_id,
            tool=name,
            args=dict(args),
            risk_level=risk if risk in ("medium", "high") else "medium",
            reason=reason,
            agent_ref=call.agent_ref,
            source_message_id=call.source_message_id,
        )

    # ------------------------------------------------------------ messages 通道

    def _translate_messages(self, parsed: _RawEvent) -> list[BaseEvent]:
        payload = parsed.payload
        if not isinstance(payload, (tuple, list)) or len(payload) != 2:
            return []
        chunk, metadata = payload
        if not isinstance(chunk, BaseMessage):
            return []
        meta = metadata if isinstance(metadata, Mapping) else {}
        agent_ref = self._agent_ref(parsed.namespace, meta)
        return self._dispatch_message(chunk, agent_ref, aggregate=False)

    def _on_chunk(self, chunk: AIMessageChunk, agent_ref: AgentRef | None) -> list[BaseEvent]:
        key = self._agent_key(agent_ref)
        open_message = self._open_message(key, agent_ref, upstream_id=chunk.id)
        events: list[BaseEvent] = []

        thinking = self._reasoning_delta(chunk)
        if thinking:
            events.append(self._delta_event(agent_ref, open_message, "thinking", thinking))
        text = chunk.text
        if text:
            events.append(self._delta_event(agent_ref, open_message, "text", text))
            open_message.emitted_content = True

        self._absorb_usage(chunk.usage_metadata, open_message)
        if getattr(chunk, "chunk_position", None) == "last":  # 安装版收口信号（U2 spike）
            events.extend(
                self._close_message(key, agent_ref, open_message, self._finish_reason(chunk))
            )
        return events

    def _on_final_message(self, message: AIMessage, agent_ref: AgentRef | None) -> list[BaseEvent]:
        """updates 通道的聚合/整段 AIMessage：收口在途消息，并为完整 tool_calls 发 started。

        - 在途消息存在 → 这就是同一条的聚合：只吸收 usage 并收口，**不重发增量**；
        - 在途不存在且是流式回合的尾巴（上游 id 已收口 / 上一次收口流式发过内容）→ 聚合
          尾巴，跳过（避免把已流出的内容再整段发一遍）；
        - 其余（真·非流式模型）→ 补一条整段 delta 再收口，保证三层测试与真模型同构。
        """
        key = self._agent_key(agent_ref)
        tool_events = [
            event
            for tool_call in message.tool_calls or []
            if (
                event := self._tool_call_started(agent_ref, tool_call, source_message_id=message.id)
            )
            is not None
        ]
        open_message = self._open_messages.get(key)
        finish_reason = self._finish_reason(message)
        if open_message is not None:
            self._absorb_usage(message.usage_metadata, open_message)
            finished = self._close_message(key, agent_ref, open_message, finish_reason)
            return finished + tool_events
        if self._is_streamed_turn_tail(message):
            return tool_events  # 聚合尾巴：消息已收口，只剩工具卡片

        # 真·非流式：整段 content 补一条 delta，再按普通消息收口
        open_message = self._open_message(key, agent_ref, upstream_id=message.id)
        text = self._message_text(message)
        events: list[BaseEvent] = []
        if text:
            events.append(self._delta_event(agent_ref, open_message, "text", text))
            open_message.emitted_content = True
        self._absorb_usage(message.usage_metadata, open_message)
        events.extend(self._close_message(key, agent_ref, open_message, finish_reason))
        return events + tool_events

    def _is_streamed_turn_tail(self, message: AIMessage) -> bool:
        """聚合消息是否是已流式回合的尾巴（只该剩工具卡片，不得整段重发）。

        两道锚均取**全局口径**（模型侧消息 id 全局唯一）——主图对 execute 子图消息的
        状态回声（namespace=() → agent_key 不同）靠它们拦截，防同文气泡双发与 usage 双计：
        - 上游 id 已收口：回声携带与内层消息相同的 id，全局集合直接命中；
        - 粘性「已流式」标志：兜上游 id 缺失（网关方言）的同源聚合尾巴；跨过工具回合
          （ToolMessage 收口）即清零——非流式模型的下一轮终答是新调用，不得误伤。
        """
        upstream_id = message.id
        if upstream_id and upstream_id in self._closed_upstream_ids:
            return True
        return self._closed_streamed and not message.tool_calls

    # ------------------------------------------------------------ updates 通道

    def _translate_updates(self, parsed: _RawEvent) -> list[BaseEvent]:
        payload = parsed.payload
        if isinstance(payload, Mapping) and "__interrupt__" in payload:
            # 审批族（M3）：approval_request 的 approval_id 由服务层审批行落库后传入，
            # 本通道不构造事件；映射入口是 describe_interrupt()（见模块 docstring）。
            return []
        agent_ref = self._agent_ref(parsed.namespace, {})
        events: list[BaseEvent] = []
        if isinstance(payload, Mapping):
            for update in payload.values():
                events.extend(self._walk_update(update, agent_ref))
        else:
            events.extend(self._walk_update(payload, agent_ref))
        return events

    def _walk_update(self, update: Any, agent_ref: AgentRef | None) -> list[BaseEvent]:
        """遍历节点状态更新，摘出消息对象（其余状态键与本层无关）。"""
        if isinstance(update, BaseMessage):
            return self._dispatch_message(update, agent_ref, aggregate=True)
        command_update = getattr(update, "update", None)  # langgraph Command(update=...) 形态
        if isinstance(command_update, Mapping):
            return self._walk_update(command_update, agent_ref)
        if isinstance(update, Mapping):
            messages = update.get("messages")
            if isinstance(messages, (list, tuple)):
                events: list[BaseEvent] = []
                for message in messages:
                    events.extend(self._walk_update(message, agent_ref))
                return events
            return []
        if isinstance(update, (list, tuple)):
            events = []
            for entry in update:
                events.extend(self._walk_update(entry, agent_ref))
            return events
        return []

    def _dispatch_message(
        self, message: Any, agent_ref: AgentRef | None, *, aggregate: bool = False
    ) -> list[BaseEvent]:
        """按通道语义分流消息对象。

        - `aggregate=True`（updates 通道）：值是**状态聚合**——即使是 AIMessageChunk 也走
          收口语义（实测 langgraph 1.2.16 的 updates 聚合消息就是带 chunk_position="last"
          的 AIMessageChunk，若按增量语义处理会把整段内容重发一遍）；
        - `aggregate=False`（messages 通道）：AIMessageChunk 是真增量。
        """
        if isinstance(message, ToolMessage):
            return self._on_tool_message(message, agent_ref)
        if isinstance(message, AIMessage):
            if aggregate or not isinstance(message, AIMessageChunk):
                return self._on_final_message(message, agent_ref)
            return self._on_chunk(message, agent_ref)
        return []

    # ------------------------------------------------------------ custom 通道

    def _translate_custom(self, parsed: _RawEvent) -> list[BaseEvent]:
        payload = parsed.payload
        if not isinstance(payload, Mapping):
            return []
        if CUSTOM_STRUCTURED_KEY in payload:
            declared = payload[CUSTOM_STRUCTURED_KEY]
            self._pending_structured = dict(declared) if isinstance(declared, Mapping) else None
            return []
        event_type = payload.get("type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            return []  # 框架杂音（非契约键）安静忽略
        fields = {key: value for key, value in payload.items() if key not in _ENVELOPE_FIELDS}
        event_cls = EVENT_TYPES[event_type]
        event = event_cls(
            seq=0,  # 占位：真实 seq 由 EventPipeline 分配
            session_id=self._session_id,
            run_id=self._run_id,
            agent=self._agent_ref(parsed.namespace, {}),
            created_at=self._now(),
            **fields,
        )
        if isinstance(event, ToolCallStartedEvent):
            # F2：custom 的 started 与 messages/updates 共用 _tool_calls 锚——先到的
            # 通道登记锚，后到的同 id 调用一律吞掉（两种顺序各恰好 1 条 started）。
            # `_finished_tool_calls` 也参与判定：resume 段历史重放里「已收口」的调用
            # 不得再次公告（否则同一 run 出现第二张工具卡）。
            if (
                event.tool_call_id in self._tool_calls
                or event.tool_call_id in self._finished_tool_calls
            ):
                return []
            self._seen_tool_call_ids.add(event.tool_call_id)
            call = _OpenToolCall(
                tool_call_id=event.tool_call_id,
                tool=event.tool,
                args=dict(event.args),
                agent_ref=event.agent,
                started_at=event.created_at,
                subagent_role=(
                    str(event.args.get("subagent_type"))
                    if event.tool == PROTOCOL_TASK_TOOL
                    else None
                ),
            )
            self._tool_calls[event.tool_call_id] = call
            if event.tool == PROTOCOL_TASK_TOOL:
                self._task_stack.append(call)
        elif isinstance(event, ToolCallFinishedEvent):
            # F2：finished 共用 _finished_tool_calls 锚（_claim_tool_finish 单一出处）。
            # tap 中间件先到的 finished 只带计时/错误分类——工具调用仍在飞（ToolMessage
            # 未到）时寄存为附言，由 _on_tool_message 合并进唯一 rich finished
            # （exit_code/result_preview/truncated 只在该通道；custom 先认领会把它们
            # 整体吞掉）。重复附言以最后一次为准（ToolRetryMiddleware 重试逐次计时）。
            open_call = self._tool_calls.get(event.tool_call_id)
            if open_call is not None:
                open_call.tap_finish = _TapFinish(
                    duration_ms=event.duration_ms, status=event.status, error=event.error
                )
                return []
            if not self._claim_tool_finish(event.tool_call_id):
                return []
        return [event]

    # ------------------------------------------------------------ 消息族装配

    def _open_message(
        self, key: str, agent_ref: AgentRef | None, *, upstream_id: str | None = None
    ) -> _OpenMessage:
        open_message = self._open_messages.get(key)
        if open_message is None:
            open_message = _OpenMessage(
                message_id=self._new_id(),
                started_at=self._now(),
                upstream_id=upstream_id,
            )
            self._open_messages[key] = open_message
        return open_message

    def _delta_event(
        self,
        agent_ref: AgentRef | None,
        open_message: _OpenMessage,
        kind: Literal["text", "thinking"],
        delta: str,
    ) -> MessageDeltaEvent:
        return MessageDeltaEvent(
            seq=0,
            session_id=self._session_id,
            run_id=self._run_id,
            agent=agent_ref,
            created_at=self._now(),
            message_id=open_message.message_id,
            kind=kind,
            delta=delta,
        )

    def _close_message(
        self,
        key: str,
        agent_ref: AgentRef | None,
        open_message: _OpenMessage,
        finish_reason: FinishReason,
        *,
        force: bool = False,
    ) -> list[BaseEvent]:
        """收口在途消息：无可见内容且无用量且无结构化结果时跳过（空工具回合不发空 finished）。"""
        self._open_messages.pop(key, None)
        closed_at = self._now()
        # 逐次记账的唯一追加点：先于 materialized 判断（0 token/空回合调用也要落行），
        # 且与消息回声去重锚共用同一收口时机（同一次调用不会被记成多行）。
        self._model_calls.append(
            ModelCallRecord(
                message_id=open_message.message_id,
                started_at=open_message.started_at,
                finished_at=closed_at,
                input_tokens=open_message.input_tokens,
                output_tokens=open_message.output_tokens,
                agent_id=agent_ref.agent_id if agent_ref is not None else None,
                role=agent_ref.role if agent_ref is not None else None,
            )
        )
        self._closed_streamed = open_message.emitted_content
        if open_message.upstream_id:
            self._closed_upstream_ids.add(open_message.upstream_id)
            if len(self._closed_upstream_ids) > 512:  # 个人体量的防膨胀上限：裁掉一半旧 id
                for stale in sorted(self._closed_upstream_ids)[
                    : len(self._closed_upstream_ids) // 2
                ]:
                    self._closed_upstream_ids.discard(stale)

        declared = open_message.structured
        if declared is None and self._pending_structured is not None:
            declared = self._pending_structured  # custom 通道先到：附着到下一条收口的消息
        self._pending_structured = None
        structured = StructuredResult(**declared) if declared is not None else None

        materialized = (
            force
            or open_message.emitted_content
            or open_message.usage_seen
            or structured is not None
        )
        if not materialized:
            return []

        usage = None
        if open_message.usage_seen:
            usage = Usage(
                input_tokens=open_message.input_tokens, output_tokens=open_message.output_tokens
            )
            self._usage_totals = Usage(
                input_tokens=self._usage_totals.input_tokens + usage.input_tokens,
                output_tokens=self._usage_totals.output_tokens + usage.output_tokens,
            )
        return [
            MessageFinishedEvent(
                seq=0,
                session_id=self._session_id,
                run_id=self._run_id,
                agent=agent_ref,
                created_at=closed_at,
                message_id=open_message.message_id,
                finish_reason=finish_reason,
                usage=usage,
                structured=structured,
            )
        ]

    def _reasoning_delta(self, chunk: AIMessageChunk) -> str | None:
        """思考流增量（ADR-007）。

        主路径 = Anthropic thinking 块：langchain-anthropic 1.7.6 流式把增量放在
        `chunk.content`（`{"type": "thinking", "thinking": ..., "index": N}`），其
        `content_blocks` 派生视图把非标准块归一化为 `{"type": "non_standard",
        "value": {...}}`——解包后按 thinking/reasoning/text 取文本；**同一 chunk 的
        多个命中块全部收集、按块序拼接**（只取首个会静默丢后续块）；空串块
        （signature-only 收口块）跳过不算增量；无块命中时回退 legacy 键。
        legacy 兜底 = `additional_kwargs["reasoning_content"]`：ChatOpenAI 的 OpenAI
        兼容口径。⚠️ "ChatOpenAI 保留 reasoning_content"已被源码实测证伪
        （langchain-openai 1.7.2 的转换函数丢弃该字段），该分支仅作兼容保留。
        """
        parts: list[str] = []
        for block in getattr(chunk, "content_blocks", None) or []:
            if not isinstance(block, Mapping):
                continue
            actual: Any = block
            if block.get("type") == "non_standard" and isinstance(block.get("value"), Mapping):
                actual = block["value"]  # langchain-core 对 thinking 块的归一化包裹
            if actual.get("type") not in ("thinking", "reasoning"):
                continue
            for key in ("thinking", "reasoning", "text"):
                text = actual.get(key)
                if isinstance(text, str) and text:
                    parts.append(text)
                    break  # 同一块只取第一个命中键，防多键双计
            # 空串块（signature-only）：不算增量，继续看下一块
        if parts:
            return "".join(parts)
        additional = chunk.additional_kwargs
        if isinstance(additional, Mapping):
            value = additional.get("reasoning_content")
            if isinstance(value, str) and value:
                return value
        return None

    def _absorb_usage(self, usage_metadata: Any, open_message: _OpenMessage) -> None:
        """吸收 usage 元数据：分项取最大值（兼容「仅末块给总量」与「每块给累计量」方言）。"""
        if not isinstance(usage_metadata, Mapping):
            return
        input_tokens = usage_metadata.get("input_tokens")
        output_tokens = usage_metadata.get("output_tokens")
        changed = False
        if isinstance(input_tokens, int):
            open_message.input_tokens = max(open_message.input_tokens, input_tokens)
            changed = True
        if isinstance(output_tokens, int):
            open_message.output_tokens = max(open_message.output_tokens, output_tokens)
            changed = True
        if changed:
            open_message.usage_seen = True

    def _finish_reason(self, message: BaseMessage) -> FinishReason:
        """finish_reason 映射：网关原值保留 stop/length/content_filter；tool_calls 等归 stop。"""
        metadata = getattr(message, "response_metadata", None)
        raw = metadata.get("finish_reason") if isinstance(metadata, Mapping) else None
        return raw if raw in ("stop", "length", "content_filter") else "stop"

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    # ------------------------------------------------------------ 工具族装配

    def _tool_call_started(
        self,
        agent_ref: AgentRef | None,
        tool_call: Any,
        *,
        source_message_id: str | None = None,
    ) -> BaseEvent | None:
        if not isinstance(tool_call, Mapping):
            tool_call = {}
        name = str(tool_call.get("name") or "")
        raw_args = tool_call.get("args")
        args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        call_id = str(tool_call.get("id") or self._new_id())
        if call_id in self._tool_calls or call_id in self._finished_tool_calls:
            # 去重：同一调用不重复发 started（三通道 + resume 历史重放）
            return None
        self._seen_tool_call_ids.add(call_id)
        risk = None
        if self._risk_level_for is not None:
            risk = self._risk_level_for(name, args)
        call = _OpenToolCall(
            tool_call_id=call_id,
            tool=name,
            args=args,
            agent_ref=agent_ref,
            started_at=self._now(),
            subagent_role=str(args.get("subagent_type")) if name == PROTOCOL_TASK_TOOL else None,
            source_message_id=source_message_id,
        )
        self._tool_calls[call_id] = call
        if name == PROTOCOL_TASK_TOOL:
            self._task_stack.append(call)
        return ToolCallStartedEvent(
            seq=0,
            session_id=self._session_id,
            run_id=self._run_id,
            agent=agent_ref,
            created_at=self._now(),
            tool_call_id=call_id,
            tool=name,
            args=args,
            risk_level=risk,
        )

    def _claim_tool_finish(self, tool_call_id: str) -> bool:
        """finished 去重锚（messages/updates/custom 三通道共用，F2）：首次认领返回 True。

        防膨胀：锚集超过 2048 时裁掉一半旧 id（长 run 口径）。
        """
        if tool_call_id in self._finished_tool_calls:
            return False
        self._finished_tool_calls.add(tool_call_id)
        if len(self._finished_tool_calls) > 2048:  # 防膨胀：长 run 裁掉一半旧 id
            for stale in sorted(self._finished_tool_calls)[:1024]:
                self._finished_tool_calls.discard(stale)
        return True

    def _on_tool_message(self, message: ToolMessage, live_ref: AgentRef | None) -> list[BaseEvent]:
        if not self._claim_tool_finish(message.tool_call_id):
            return []  # 去重锚：同一调用在 messages/updates/custom 三通道只收口一次（F2）
        self._seen_tool_call_ids.add(message.tool_call_id)
        self._closed_streamed = False  # 工具回合边界：粘性标志只服务当前回合的聚合尾巴
        call = self._tool_calls.pop(message.tool_call_id, None)
        # 工具卡片归属跟随其 started 事件（task 调用自身的卡片归父智能体）
        agent_ref = call.agent_ref if call is not None else live_ref
        content = self._message_text(message)
        status: Literal["success", "error"] = "error" if message.status == "error" else "success"

        error = None
        if status == "error":
            # ToolMessage 错误只有正文（异常对象已丢失）：按 deepagents 文案分类
            error = classify_tool_text(content, idempotent=self._is_idempotent(call))

        exit_code, artifact_truncated, artifact_ref = self._artifact_facts(message)
        if exit_code is None:  # 安装版 deepagents 不写 artifact：回退解析正文标记（0.6.12 实测）
            marker = _EXIT_CODE_MARKER.search(content)
            if marker:
                exit_code = int(marker.group(1))
        text_truncated = _TRUNCATION_MARKER in content
        offload_ref = None
        match = _OFFLOAD_PATH_MARKER.search(content)
        if match:
            offload_ref = match.group(1)

        truncated = text_truncated or artifact_truncated or len(content) > self._preview_limit
        todos = self._todos_snapshot(call)

        finished = ToolCallFinishedEvent(
            seq=0,
            session_id=self._session_id,
            run_id=self._run_id,
            agent=agent_ref,
            created_at=self._now(),
            tool_call_id=message.tool_call_id,
            status=status,
            # 计时权威源 = tap 附言（实测单次执行时长）；无附言回退锚起点推算（裸路径）。
            # 三元内联在参数列表里：假时钟按 now() 调用次数 +1ms，求值次序影响 golden。
            duration_ms=(
                call.tap_finish.duration_ms
                if call is not None and call.tap_finish is not None
                else self._duration_ms(call)
            ),
            result_preview=content[: self._preview_limit] or None,
            truncated=truncated,
            exit_code=exit_code,
            result_ref=artifact_ref or offload_ref,
            error=error,
            todos=todos,
        )
        if (
            call is not None
            and call.tool == PROTOCOL_TASK_TOOL
            and self._task_stack
            and self._task_stack[-1].tool_call_id == call.tool_call_id
        ):
            # task 调用收口：对应子智能体作用域出栈（后续事件回到父作用域）
            self._task_stack.pop()
        return [finished]

    def _flush_open_tool_calls(self) -> list[BaseEvent]:
        """run 收口时仍在飞的工具调用补收口（tap 附言优先；无附言按运行结局合成错误）。

        两条真实路径需要它：tap 的 finished 附言寄存后 ToolMessage 永不到（异常逃出
        工具节点——安装版 langchain 默认只把验证错误转 error ToolMessage，其余原样
        上抛 → run error）；stop 打断执行中的工具调用（连附言都没有）。不补收口，
        工具卡片将永远悬着。
        """
        events: list[BaseEvent] = []
        for call_id, call in list(self._tool_calls.items()):
            if not self._claim_tool_finish(call_id):
                continue
            self._tool_calls.pop(call_id, None)
            tap = call.tap_finish
            if tap is not None and tap.error is not None:
                status: Literal["success", "error"] = "error"
                error: ToolErrorObj | None = tap.error
            elif tap is not None and tap.status == "success":
                status = "success"
                error = None
            else:
                status = "error"
                error = self.classify_tool_failure(tool=call.tool)
            events.append(
                ToolCallFinishedEvent(
                    seq=0,
                    session_id=self._session_id,
                    run_id=self._run_id,
                    agent=call.agent_ref,
                    created_at=self._now(),
                    tool_call_id=call_id,
                    status=status,
                    duration_ms=tap.duration_ms if tap is not None else 0,
                    result_preview=None,
                    truncated=False,
                    exit_code=None,
                    result_ref=None,
                    error=error,
                    todos=self._todos_snapshot(call),
                )
            )
            if (
                call.tool == PROTOCOL_TASK_TOOL
                and self._task_stack
                and self._task_stack[-1].tool_call_id == call_id
            ):
                self._task_stack.pop()  # task 调用随 run 收口出栈（后续事件回到父作用域）
        return events

    def _duration_ms(self, call: _OpenToolCall | None) -> int:
        if call is None or call.started_at is None:
            return 0
        return max(int((self._now() - call.started_at).total_seconds() * 1000), 0)

    def _is_idempotent(self, call: _OpenToolCall | None) -> bool:
        """工具是否幂等：白名单内 True，其余 False（保守口径——retryable 建议宁缺毋滥）。"""
        return call is not None and call.tool in self._idempotent_tools

    def _artifact_facts(self, message: ToolMessage) -> tuple[int | None, bool, str | None]:
        """从 artifact（协议内置）提取 exit_code / 截断 / 落盘引用；缺失回退正文标记。"""
        artifact = message.artifact
        if not isinstance(artifact, Mapping):
            return None, False, None
        exit_code = artifact.get("exit_code")
        truncated = artifact.get("truncated") is True
        result_ref = None
        for key in ("capture_path", "file_path", "path", "result_ref"):
            value = artifact.get(key)
            if isinstance(value, str) and value:
                result_ref = value
                break
        return (
            exit_code if isinstance(exit_code, int) else None,
            truncated,
            result_ref,
        )

    def _todos_snapshot(self, call: _OpenToolCall | None) -> list[TodoItem] | None:
        """write_todos 的全量 todo 快照（REQ-005 面板数据源）；非法条目丢弃保事件合法。"""
        if call is None or call.tool != PROTOCOL_WRITE_TODOS_TOOL:
            return None
        raw = call.args.get("todos")
        if not isinstance(raw, (list, tuple)):
            return None
        items: list[TodoItem] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            content = entry.get("content")
            status = entry.get("status")
            if isinstance(content, str) and status in _TODO_STATUSES:
                # status 被 `in _TODO_STATUSES` 限定为契约三值；mypy 不追踪容器成员测试
                items.append(TodoItem(content=content, status=status))
        return items or None

    # ------------------------------------------------------------ 运行状态族

    def _run_status_event(
        self,
        phase: Literal["started", "finished", "stopped", "error"],
        *,
        subject: Literal["run", "compact"] = "run",
        error: Any = None,
        usage_totals: Usage | None = None,
    ) -> RunStatusEvent:
        return RunStatusEvent(
            seq=0,
            session_id=self._session_id,
            run_id=self._run_id,
            agent=None,  # run_status 永远是主智能体视角（子智能体压缩暂不在 P0）
            created_at=self._now(),
            subject=subject,
            phase=phase,
            error=error,
            usage_totals=usage_totals,
        )

    def classify_run_failure(self, exc: BaseException | None = None) -> Any:
        """运行异常 → `RunErrorObj`（给 runtime 的 stop/异常收敛路径复用，A3 §5.5 第 4 步）。"""
        return classify_run_error(exc)

    def classify_tool_failure(
        self, exc: BaseException | None = None, *, tool: str | None = None
    ) -> Any:
        """工具异常 → `ToolErrorObj`（middleware 通道没有 ToolMessage 时的分类复用）。"""
        idempotent = tool in self._idempotent_tools if tool else False
        return classify_tool_error(exc, idempotent=idempotent)

    # ------------------------------------------------------------ AgentRef 归属推导（U2）

    def _agent_ref(
        self, namespace: tuple[str, ...], metadata: Mapping[str, Any]
    ) -> AgentRef | None:
        segments = self._ns_segments(namespace, metadata)
        task = self._task_stack[-1] if self._task_stack else None
        role = (task.subagent_role if task else None) or self._role_from_names(segments, metadata)
        # 子作用域判定（两种形态的口径不同，见 _ns_segments）：
        # - namespace 元组形态（subgraphs=True）：元组非空 = 处在子图/子智能体作用域；
        # - metadata 形态：checkpoint_ns 恒带根图节点段，≥2 段才说明有嵌套作用域。
        in_sub_scope = bool(namespace) or len(segments) > 1
        if task is None and not in_sub_scope:
            return None  # 主智能体：无 task 栈且非嵌套命名空间
        if namespace:
            agent_id = "|".join(segments)  # 元组本身就是子作用域路径（根图贡献空元组）
        elif len(segments) > 1:
            agent_id = "|".join(segments[1:])  # 剥掉根图节点段
        else:
            # 走到这里必然有 task 栈（in_sub_scope 为假时上面已返回 None）
            agent_id = f"task:{task.tool_call_id}"  # type: ignore[union-attr]
        parent_id = None
        if len(self._task_stack) >= 2:  # 嵌套 task：父 = 外层帧
            outer = self._task_stack[-2]
            parent_id = f"task:{outer.tool_call_id}"
        return AgentRef(
            agent_id=agent_id,
            role=role,
            parent_agent_id=parent_id,
            spawned_by_tool_call_id=task.tool_call_id if task else None,
        )

    def _ns_segments(
        self, namespace: tuple[str, ...], metadata: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """子图命名空间路径段：优先 astream 的 namespace 元组，回退 checkpoint_ns 键。"""
        if namespace:
            return tuple(str(part) for part in namespace if str(part))
        for key in _CHECKPOINT_NS_KEYS:  # U2 实测键（安装版 langgraph 1.2.16）
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return tuple(part for part in value.split("|") if part)
        return ()

    def _role_from_names(
        self, segments: tuple[str, ...], metadata: Mapping[str, Any]
    ) -> str | None:
        candidates = [str(metadata.get("langgraph_node") or "")]
        for segment in segments:
            name, _, _ = segment.partition(":")
            candidates.append(name)
        for candidate in candidates:
            if candidate in self._subagent_roles:
                return candidate
        return None

    @staticmethod
    def _agent_key(agent_ref: AgentRef | None) -> str:
        """agent 作用域键：在途消息按子智能体隔离（主智能体 = 空串）。"""
        return agent_ref.agent_id if agent_ref is not None else ""

    def _ref_of(self, key: str) -> AgentRef | None:
        """收口时回溯该作用域的归属（close_open_message 的 force 路径没有 live ref）。"""
        if not key:
            return None
        task = self._task_stack[-1] if self._task_stack else None
        return AgentRef(
            agent_id=key,
            role=task.subagent_role if task else None,
            parent_agent_id=None,
            spawned_by_tool_call_id=task.tool_call_id if task else None,
        )
