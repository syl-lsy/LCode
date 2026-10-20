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
- tail 是 deepagents 生成的（我们无权排列）；M2 不装 HITL（permissions/interrupt_on
  不传入，见 `permissions.py` 模块注释；M3 以 `interrupt_on=` 通路接线）。
- P1 的 `HookBridgeMiddleware` 预留插槽 = HITL 之前（单一闸门原则，硬约束 11）。

**compact 事件现状**：设计 §6.2 的 `run_status(subject=compact)` 依赖 0.7.25 快照的
`SummarizationEvent` state 键——安装版 langchain 1.4.6 的 `SummarizationMiddleware`
**不写**该键（本车道实测，源码无 `summarization_event`）。M3 做压缩事件时改用
`wrap_model_call` 前后计数方案，M2 不产 compact。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_stream_writer

from app.utils.error_classifier import classify_tool_error, classify_tool_text
from app.utils.exec_context import EXEC_ID_CONTEXT

IDEMPOTENT_TOOLS: tuple[str, ...] = ("read_file", "ls", "glob", "grep")
"""ToolRetryMiddleware 幂等白名单（ADR-002 仅幂等工具重试；permissions.READ_ONLY_TOOLS 同源）。"""

WRITE_TODOS_TOOL: str = "write_todos"
"""todo 工具名（langchain TodoListMiddleware 内置；translator 同名约定 PROTOCOL_WRITE_TODOS_TOOL，
agent/ 禁止 import events/ 故此处独立声明，两处必须一致）。"""


def _tool_name(tool: Any) -> str | None:
    """BaseTool / dict 工具的名字提取（deepagents `_tool_exclusion` 同款逻辑）。"""
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


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

    只实现 `awrap_tool_call`：M2 外层图恒以 async 跑（RunExecutor.astream），
    同步执行路径不经过本中间件（默认放行，不影响行为面）。
    """

    name = "LCodeEventTapMiddleware"

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
