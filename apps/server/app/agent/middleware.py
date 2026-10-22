"""LCode 业务 middleware：事件 tap / 工具重试 / 子智能体工具面收敛（A3 §5.2）。

**栈序（以安装版 deepagents 0.6.12 `graph.py` 为准，快照 0.7.25 的结论作废）**：

- 默认栈（`deepagents/graph.py:773-804`）**已含 `TodoListMiddleware`**
  （栈首，`write_todos` 默认可用）→ LCode **不再装配** TodoList，重复装配会产生
  重复工具定义（AGENTS.md 硬约束 2 的 0.6.12 口径）；
- 用户 middleware 在默认栈**之后**、tail（prompt caching / Memory / HITL）**之前**
  追加（`graph.py:811-812` `deepagent_middleware.extend(middleware)`；0.6.12 没有
  快照 0.7.25 的 `_apply_custom_middleware` name 合并语义）；
- 本模块清单（列表序即装配序，名单制——之外的 middleware 一律不加）：
  1. `ToolRetryMiddleware`（langchain 自带，幂等白名单只放只读工具，ADR-002）；
  2. `LCodeEventTapMiddleware`（计时/错误分类/自定义流）。
  tap 放在 retry 之内（列表更后 = 更贴近工具）：计时测的是**单次真实执行**，
  重试由外层逐次包裹、每次尝试各自计时。
- tail 是 deepagents 生成的（我们无权排列）；M3 起 `interrupt_on=` 由 assembly 传入，
  HITL（`HumanInTheLoopMiddleware`）装在该 tail（`graph.py:830-835`）——本模块**不自装**
  HITL（Phase 3 的 AUTO 分类器需要 after_model 顺序时再改，见 m3-spikes §1.2/§5）。
- P1 的 `HookBridgeMiddleware` 预留插槽 = HITL 之前（单一闸门原则，硬约束 11）。

**compact 事件（M3 Phase 5，AC-010-1）**：`awrap_model_call` 检测上下文压缩，经 custom
流发 `run_status(subject=compact)`（started→finished 成对，finished 带 `CompactInfo`）。
不新增 wire 事件类型——载荷走 translator 的 custom 通道（`_translate_custom` 补信封后过
契约校验，见 `events/translator.py` 模块 docstring 的协议键口径）。

检测点有两条路（相互兜底，见 `_detect_in_call` / `_detect_from_result`）：

1. **入口窗口比对**（安装版 deepagents 0.6.12 的**实际**生效路径）：摘要中间件在用户
   middleware **之外**——`graph.py:801` 的默认栈先于 `graph.py:812` 的用户追加，
   而 langchain `_chain_model_call_handlers` 口径是「first in list becomes outermost」
   （`langchain/agents/factory.py:286`）。它把压缩后的 effective 窗口交给内层，
   本 tap 在入口看到的就是 `[summary, *preserved]`；与「state 消息 + 上一个
   `_summarization_event` 重建的窗口」比对即可判定本次调用发生了压缩，before/after
   两个计数都能精确重建（普通调用只做 O(1) 引用比对，不计数）。
2. **结果事件**（m3-spikes §3 的主路径，适用于 tap 在摘要中间件**外侧**的装配）：
   `ExtendedModelResponse.command.update["_summarization_event"]`（deepagents
   `summarization.py:1501/1636` 只在真正压缩时写该键；`truncate_args` 的纯参数截断
   不算压缩，见 `summarization.py:1432/1566` 早退分支）。

去重键 = `SummarizationEvent.cutoff_index`（state 绝对索引；链式摘要由
`_compute_state_cutoff` 换算，`summarization.py:859-884`）——同一压缩只发一对事件。
任何检测/发射异常都只记 WARNING 并按「无 compact 事件」降级：绝不让 run 因压缩提示
失败而 error。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_stream_writer

from app.core.logger import BusinessLog
from app.utils.error_classifier import classify_tool_error, classify_tool_text
from app.utils.exec_context import EXEC_ID_CONTEXT

IDEMPOTENT_TOOLS: tuple[str, ...] = ("read_file", "ls", "glob", "grep")
"""ToolRetryMiddleware 幂等白名单（ADR-002 仅幂等工具重试；permissions.READ_ONLY_TOOLS 同源）。"""

WRITE_TODOS_TOOL: str = "write_todos"
"""todo 工具名（langchain TodoListMiddleware 内置；translator 同名约定 PROTOCOL_WRITE_TODOS_TOOL，
agent/ 禁止 import events/ 故此处独立声明，两处必须一致）。"""

SUMMARIZATION_EVENT_KEY: str = "_summarization_event"
"""deepagents 0.6.12 摘要事件私有 state 键（`summarization.py:209` 声明、`1501` 写入）。

`SummarizationEvent` = `{cutoff_index: int, summary_message: HumanMessage, file_path: str | None}`
（`summarization.py:147-157`）；`cutoff_index` 是 **state 消息列表的绝对索引**。
"""


def _tool_name(tool: Any) -> str | None:
    """BaseTool / dict 工具的名字提取（deepagents `_tool_exclusion` 同款逻辑）。"""
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


# ---------------------------------------------------------------- compact 检测（AC-010-1）


@dataclass(frozen=True, slots=True)
class _CompactTrigger:
    """一次上下文压缩的检测结果（去重键 = state 绝对 `cutoff_index`）。"""

    cutoff_index: int
    before_tokens: int
    after_tokens: int


@dataclass(frozen=True, slots=True)
class _ModelCallSnapshot:
    """模型调用入口快照（普通调用不计数：只有疑似压缩时才推进数值计算）。

    - `request_messages`：本次模型调用实际使用的窗口（可能已被外层摘要改写）；
    - `state_messages`：原始 state 消息（压缩前的完整历史，`state["messages"]`）；
    - `prior_valid`：入口可见的上一个摘要事件解析结果 `(cutoff_index, summary_message)`，
      形状不符为 None（本次调用**之前**已落地的事件，不是本次的结果）。
    """

    request_messages: list[BaseMessage]
    state_messages: list[BaseMessage] | None
    prior_valid: tuple[int, BaseMessage] | None
    prior_malformed: bool = False


def _compact_warning(message: str, exc: Exception | None = None) -> None:
    """压缩提示降级留痕（WARNING；绝不上抛——提示是附属信息，运行优先）。"""
    BusinessLog(message=message, action="检测上下文压缩", entity="run").warning(exc)


def _state_view(request: ModelRequest[Any]) -> Mapping[str, Any]:
    """`request.state` 以 Mapping 面读取（AgentState TypedDict 不含私有键，运行期可见）。"""
    return cast(Mapping[str, Any], request.state)


def _state_messages(request: ModelRequest[Any]) -> list[BaseMessage] | None:
    """state 原始消息列表（全量 BaseMessage 才认；其余形态按不可用降级，不裁剪下标）。"""
    raw = _state_view(request).get("messages")
    if not isinstance(raw, (list, tuple)):
        return None
    if not all(isinstance(message, BaseMessage) for message in raw):
        return None
    return list(raw)


def _parse_summarization_event(event: Mapping[str, Any]) -> tuple[int, BaseMessage] | None:
    """`SummarizationEvent` → `(cutoff_index, summary_message)`；畸形返回 None 并留 WARNING。

    `cutoff_index <= 0` 是「未发生压缩」的合法形态（deepagents
    `summarization.py:1440-1443`：cutoff<=0 直接早退、不写事件键），不告警。
    """
    cutoff = event.get("cutoff_index")
    summary = event.get("summary_message")
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff <= 0:
        _compact_warning("摘要事件 cutoff_index 缺失或非法（本次不产 compact 事件）")
        return None
    if not isinstance(summary, BaseMessage):
        _compact_warning("摘要事件 summary_message 缺失或非法（本次不产 compact 事件）")
        return None
    return cutoff, summary


def _same_message(left: BaseMessage, right: BaseMessage) -> bool:
    """同一条消息判定：对象同一优先，id 相等次之（`model_copy` 保留 id，截断不改 id）。"""
    if left is right:
        return True
    left_id = getattr(left, "id", None)
    right_id = getattr(right, "id", None)
    return left_id is not None and left_id == right_id


def _snapshot_model_call(request: ModelRequest[Any]) -> _ModelCallSnapshot:
    """入口快照（只存引用与解析结果；不做 token 计数，普通调用零成本）。

    键存在但形状不符 → `prior_malformed`：入口窗口路径整体降级（不基于畸形事件猜
    窗口重建口径），结果事件路径仍可独立工作。
    """
    raw_event = _state_view(request).get(SUMMARIZATION_EVENT_KEY)
    prior_valid: tuple[int, BaseMessage] | None = None
    prior_malformed = False
    if raw_event is not None:
        if isinstance(raw_event, Mapping):
            prior_valid = _parse_summarization_event(raw_event)
            prior_malformed = prior_valid is None
        else:
            _compact_warning("state 中的摘要事件形状非法（本次不产 compact 事件）")
            prior_malformed = True
    return _ModelCallSnapshot(
        request_messages=list(request.messages),
        state_messages=_state_messages(request),
        prior_valid=prior_valid,
        prior_malformed=prior_malformed,
    )


def _effective_before(snapshot: _ModelCallSnapshot) -> list[BaseMessage] | None:
    """压缩前的 effective 窗口：`[prior_summary, *state[prior_cutoff:]]`（无事件 = state 全量）。

    与 deepagents `_apply_event_to_messages`（`summarization.py:803-856`）同一重建口径；
    state 不可用 / cutoff 越界时返回 None（不猜）。
    """
    messages = snapshot.state_messages
    if messages is None:
        return None
    if snapshot.prior_valid is None:
        return list(messages)
    cutoff, summary = snapshot.prior_valid
    if cutoff > len(messages):
        _compact_warning("摘要事件 cutoff_index 超出 state 消息数（本次不产 compact 事件）")
        return None
    return [summary, *messages[cutoff:]]


def _count_tokens(messages: Sequence[BaseMessage]) -> int:
    """近似 token 计数（langchain-core 的字符启发式，无分词器、无网络）。"""
    return max(int(count_tokens_approximately(list(messages))), 0)


def _detect_in_call(
    request: ModelRequest[Any], snapshot: _ModelCallSnapshot
) -> _CompactTrigger | None:
    """入口窗口比对（安装版栈序下**实际生效**的检测路径，见模块 docstring）。

    摘要中间件在 tap 外侧：压缩发生时它把 `handler(request.override(messages=...))`
    改成 `[new_summary, *preserved]` 再交给内层，所以入口的 `request.messages` 已经
    是压缩后的窗口，而 event 键要等模型节点后的 `Command` 才落地。判定依据：
    - 有上一个事件：窗口首条仍是那个 summary（对象/id 相同）→ 本次未压缩；
    - 无上一个事件：窗口与 state 首条相同且长度不缩 → 本次未压缩
      （`truncate_args` 只改 tool args、`model_copy` 保留 id，不会误判）；
    - 其余 → 本次发生了压缩：`eff_cutoff = 1 + len(before) - len(after)`，
      state 绝对 cutoff = `prior_cutoff + eff_cutoff - 1`（无事件时即 eff_cutoff），
      与 deepagents `_compute_state_cutoff`（`summarization.py:859-884`）一致。
    """
    if snapshot.prior_malformed:
        return None  # 畸形事件不参与窗口重建（按「无 compact 事件」降级，不猜 cutoff）
    messages = snapshot.request_messages
    if not messages:
        return None
    prior = snapshot.prior_valid
    state_messages = snapshot.state_messages
    if prior is not None:
        if _same_message(messages[0], prior[1]):
            return None
    else:
        if state_messages is None or not state_messages:
            return None
        if _same_message(messages[0], state_messages[0]) or len(messages) > len(state_messages):
            return None
    effective_before = _effective_before(snapshot)
    if effective_before is None:
        return None
    effective_cutoff = len(effective_before) - len(messages) + 1
    if effective_cutoff <= 0:
        return None  # 窗口反而变长：不是摘要中间件的改写（不猜）
    cutoff_index = effective_cutoff if prior is None else prior[0] + effective_cutoff - 1
    return _CompactTrigger(
        cutoff_index=cutoff_index,
        before_tokens=_count_tokens(effective_before),
        after_tokens=_count_tokens(messages),
    )


def _detect_from_result(request: ModelRequest[Any], result: Any) -> _CompactTrigger | None:
    """结果事件检测（m3-spikes §3 主路径；tap 在摘要中间件外侧时生效）。

    `CompactInfo` 口径（AC-010-1）：`before_tokens = count(request.messages)`（外层看到
    的压缩前窗口）、`after_tokens = count([summary, *messages[cutoff:]])`。
    """
    if not isinstance(result, ExtendedModelResponse):
        return None
    command = result.command
    update = getattr(command, "update", None) if command is not None else None
    if not isinstance(update, Mapping):
        return None
    raw = update.get(SUMMARIZATION_EVENT_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _compact_warning("摘要事件形状非法（本次不产 compact 事件）")
        return None
    parsed = _parse_summarization_event(raw)
    if parsed is None:
        return None
    cutoff_index, summary = parsed
    if cutoff_index > len(request.messages):
        _compact_warning("摘要事件 cutoff_index 超出请求消息数（本次不产 compact 事件）")
        return None
    after_messages = [summary, *request.messages[cutoff_index:]]
    return _CompactTrigger(
        cutoff_index=cutoff_index,
        before_tokens=_count_tokens(request.messages),
        after_tokens=_count_tokens(after_messages),
    )


def _emit_compact(trigger: _CompactTrigger) -> None:
    """经 custom 流发 started→finished。

    契约约束（`RunStatusEvent`）：subject=compact 只有 started/finished，finished 必须
    携带 compact 前后窗口占用；stopped 不存在（validator 会拒）。
    """
    writer = get_stream_writer()
    writer({"type": "run_status", "subject": "compact", "phase": "started"})
    writer(
        {
            "type": "run_status",
            "subject": "compact",
            "phase": "finished",
            "compact": {
                "before_tokens": trigger.before_tokens,
                "after_tokens": trigger.after_tokens,
            },
        }
    )


class LCodeEventTapMiddleware(AgentMiddleware):
    """工具调用 tap：计时、错误分类、经 `get_stream_writer()` 发自定义流（§6.2）。

    分层纪律（A3 §1.2 禁止 `agent/ → events/`）：本中间件只经 LangGraph 框架设施
    （stream writer）发自定义载荷，不 import 业务事件包。载荷走 translator 的
    custom 通道协议：`{"type": "tool_call_started"/"tool_call_finished", ...}`
    （`events/translator.py::_translate_custom` 消费，补信封后过契约校验）。

    与 translator 的双通道关系：translator 同时从 updates 通道推导 started/finished
    （含 ToolMessage 结果），本 tap 是**计时/错误分类的权威源**（updates 无计时）。
    同一 tool_call_id 的去重收敛是流式车道（run_service 接线）的职责——translator
    已有 `_finished_tool_calls` 去重锚。M2 用 integration 用例固化载荷形态。

    只实现 async 钩子（`awrap_tool_call` / `awrap_model_call`）：M2 外层图恒以 async 跑
    （RunExecutor.astream），同步执行路径不经过本中间件（默认放行，不影响行为面）。

    `awrap_model_call` 只做压缩检测（AC-010-1）：普通调用原样透传、零事件、零计数；
    检测到压缩才发 `run_status(subject=compact)` 对（去重键 = cutoff_index）。
    """

    name = "LCodeEventTapMiddleware"

    def __init__(self) -> None:
        # 同一压缩只发一对事件（started+finished）；实例按 run 段重建（assembly 每次 run
        # 新建 agent），跨段去重由「上一段已发、本段入口窗口首条 == 该 summary」自然成立。
        self._last_compact_cutoff: int | None = None

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """模型调用透明壳：检测压缩并（仅此时）经 custom 流发 compact 事件。

        永不改变主流程：快照/检测/发射任一环节异常都只记 WARNING 并原样返回模型响应。
        """
        snapshot: _ModelCallSnapshot | None = None
        try:
            snapshot = _snapshot_model_call(request)
        except Exception as exc:  # noqa: BLE001 —— 检测面故障不得拖垮模型调用
            _compact_warning("压缩检测入口快照失败（本次不产 compact 事件）", exc)

        result = await handler(request)

        if snapshot is None:
            return result
        try:
            trigger = _detect_in_call(request, snapshot)
            if trigger is None:
                trigger = _detect_from_result(request, result)
        except Exception as exc:  # noqa: BLE001 —— 同上：降级为无 compact 事件
            _compact_warning("上下文压缩检测失败（本次不产 compact 事件）", exc)
            return result
        if trigger is None or trigger.cutoff_index == self._last_compact_cutoff:
            return result
        try:
            _emit_compact(trigger)
        except Exception as exc:  # noqa: BLE001 —— 发射失败同样只降级（run 照常继续）
            _compact_warning("compact 事件发射失败（运行继续，本段无压缩提示）", exc)
            return result
        self._last_compact_cutoff = trigger.cutoff_index
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        call = request.tool_call
        tool_call_id = str(call.get("id") or "")
        tool = str(call.get("name") or "")
        args = call.get("args") or {}

        writer = get_stream_writer()
        writer(
            {
                "type": "tool_call_started",
                "tool_call_id": tool_call_id,
                "tool": tool,
                "args": args,
            }
        )

        started = time.monotonic()
        # exec_id 注解（AC-002-1）：deepagents 0.6.12 的 execute 工具不向 backend 传
        # exec_id（LCode 扩展参数），工具调用期间写 ContextVar，LcodeSandbox.run_command
        # 缺省时回退读取——沙箱输出块据此标注来源 tool_call_id（任务上下文隔离，并行
        # 工具调用互不串线）。空 id 归一化为 None（契约 exec_id 是 str | None）。
        token = EXEC_ID_CONTEXT.set(tool_call_id or None)
        try:
            try:
                result = await handler(request)
            except Exception as exc:
                error = classify_tool_error(exc, idempotent=tool in IDEMPOTENT_TOOLS)
                writer(
                    {
                        "type": "tool_call_finished",
                        "tool_call_id": tool_call_id,
                        "status": "error",
                        "duration_ms": _elapsed_ms(started),
                        "error": error.model_dump(),
                    }
                )
                raise
            error = _classify_result_error(result, tool)
            payload: dict[str, Any] = {
                "type": "tool_call_finished",
                "tool_call_id": tool_call_id,
                "status": "error" if error is not None else "success",
                "duration_ms": _elapsed_ms(started),
            }
            # execute 类命令的退出码走 ToolMessage.artifact（ExecuteResponse，协议内置）：
            # 非 0 不代表工具失败（deepagents 口径：命令跑了就是 success，读码判命令失败）
            artifact = getattr(result, "artifact", None)
            exit_code = getattr(artifact, "exit_code", None)
            if exit_code is not None:
                payload["exit_code"] = exit_code
            # write_todos 的全量快照随 finished 携带（REQ-005 面板数据源，§6.2：快照取工具入参）
            if tool == WRITE_TODOS_TOOL and isinstance(args.get("todos"), list):
                payload["todos"] = args["todos"]
            if error is not None:
                payload["error"] = error.model_dump()
            writer(payload)
            return result
        finally:
            EXEC_ID_CONTEXT.reset(token)


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _classify_result_error(result: Any, tool: str) -> Any:
    """工具返回值里的错误面：异常走 except 分支，这里收 ToolMessage 的 status=error。"""
    if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
        # 正文已面向模型、非 SDK 原文（NFR-004 允许保留）；幂等性按白名单给建议
        return classify_tool_text(str(result.content), idempotent=tool in IDEMPOTENT_TOOLS)
    return None


def build_tool_retry_middleware() -> ToolRetryMiddleware:
    """ADR-002：仅幂等白名单重试，单次立即重试（退避交给上层 service 语义）。"""
    return ToolRetryMiddleware(
        max_retries=1,
        tools=list(IDEMPOTENT_TOOLS),
        initial_delay=0.0,
        backoff_factor=0.0,
    )


class ToolExclusionMiddleware(AgentMiddleware):
    """按名收敛模型可见工具面（explorer/reviewer 的只读面；AC-016-3 的模型侧保证）。

    deepagents 0.6.12 的同类物是私有的 `_ToolExclusionMiddleware`
    （`deepagents/middleware/_tool_exclusion.py`，harness profile 专用）——LCode
    不 import 私有件，按同一机制（`wrap_model_call` 里 `request.override(tools=...)`
    过滤）自持一份公开实现。子智能体不可见的工具即不可调用（模型面）；配合提示词
    约束构成"无写工具而非仅提示词"的双重保证的**模型侧一半**——工具级 deny 要等
    deepagents 支持沙箱 backend 的 permissions（见 `permissions.py` 模块注释）。
    """

    name = "LCodeToolExclusionMiddleware"

    def __init__(self, excluded: tuple[str, ...]) -> None:
        self._excluded = frozenset(excluded)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._filtered(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage:
        return await handler(self._filtered(request))

    def _filtered(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        if not self._excluded:
            return request
        visible = [t for t in request.tools if _tool_name(t) not in self._excluded]
        return request.override(tools=visible)


__all__ = [
    "IDEMPOTENT_TOOLS",
    "LCodeEventTapMiddleware",
    "ToolExclusionMiddleware",
    "build_tool_retry_middleware",
]
