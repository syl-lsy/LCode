"""异常分类器：内部异常 / SDK 异常 → 事件侧错误码 + retryable（A3 §8.1 末段）。

职责边界（`contracts/src/lcode_contracts/errors.py` 三注册表互不混用）：
- 本模块只产**事件侧**两个命名空间——`ToolErrorCode`(9 值) → `tool_call_finished.error`
  / `browser_action.error`，`RunErrorCode`(5 值) → `run_status(phase=error).error`；
- REST 错误码（`RestErrorCode` + ERROR_MAP）在 `app/exception/`，本模块**不碰**；
- `retryable` 由「错误分类 + 工具幂等性」共同决定（NFR-009 口径）：瞬态类错误码
  （`TRANSIENT_TOOL_CODES`）且工具非「明确不幂等」才为 True。

识别途径（三条，按序命中即止）——为什么不是单一 isinstance：
1. **register 挂钩**：第一方领域异常（如 sandbox 车道的 `SandboxUnavailable`）由**其所在
   模块**调用 `register_tool_error()` 登记。本包（`events/`+`utils/`）不得 import
   `app/sandbox`（A3 §1.2 依赖禁令），注册表是反向注册的接缝。
2. **类名表**：跨包 SDK 异常（e2b / OpenSandbox / MCP）以 `type(exc).__name__` 精确匹配
   ——同样为了不 import 它们的包。命中清单见 `_TOOL_NAME_RULES` / `_MODEL_ERROR_NAMES`。
3. **类型规则**：stdlib 语义明确的异常（`asyncio.CancelledError` → cancelled、
   `TimeoutError`/`socket.timeout` → timeout、`FileNotFoundError` → not_found、
   `PermissionError` → permission_denied、`ValueError`/`TypeError` → param_invalid；
   pydantic 的 `ValidationError` 是 `ValueError` 子类，同归 param_invalid）。

密钥安全（NFR-004 / A3 §8.1 末段）：SDK 原始异常文本常含 key 片段 / endpoint / prompt，
**绝不** `str(exc)` 进 wire——`message` 默认只用固定文案 + 异常**类名**；调用方确知安全时
（如 deepagents 已面向模型的 ToolMessage 正文）可显式传 `message=`。
"""

import asyncio
from collections.abc import Mapping
from typing import get_args

from lcode_contracts.errors import RunErrorCode, RunErrorObj, ToolErrorCode, ToolErrorObj

# ---------------------------------------------------------------- 注册表与常量

TOOL_ERROR_CODES: frozenset[str] = frozenset(get_args(ToolErrorCode))
"""契约 ToolErrorCode 的 9 个值（从契约 Literal 派生，契约改则此处自动同步）。"""

RUN_ERROR_CODES: frozenset[str] = frozenset(get_args(RunErrorCode))
"""契约 RunErrorCode 的 5 个值。"""

TRANSIENT_TOOL_CODES: frozenset[str] = frozenset(
    {"timeout", "sandbox_unavailable", "mcp_transport_error"}
)
"""瞬态（可重试候选）工具错误码：基础设施抖动，重试有意义；其余码确定性失败。"""

_TRANSIENT_RUN_CODES: frozenset[str] = frozenset({"rate_limited"})
"""瞬态（可重试候选）运行错误码：限流退避后重试有意义。"""

_TRANSIENT_MODEL_ERROR_NAMES: frozenset[str] = frozenset(
    {"APIConnectionError", "APIConnectionTimeoutError", "APITimeoutError", "InternalServerError"}
)
"""模型网关调用失败里的瞬态类（其余如鉴权/参数类是确定性失败，重试只会再错）。"""

_TOOL_NAME_RULES: dict[str, str] = {
    # 沙箱 SDK（e2b==2.31.0 / OpenSandbox 1.1.x）——跨包，不 import，按类名识别
    "SandboxException": "sandbox_unavailable",
    "SandboxError": "sandbox_unavailable",
    "SandboxUnavailable": "sandbox_unavailable",
    "SandboxUnavailableError": "sandbox_unavailable",
    "SandboxNotRunningError": "sandbox_unavailable",
    "SandboxCrashError": "sandbox_unavailable",
    "SandboxNotFoundException": "sandbox_unavailable",
    "CommandTimeoutError": "timeout",
    "TimeoutException": "timeout",
    # MCP（P1 接入；码值语义见 contracts/errors.py：transoport=模型无法行动类）
    "McpTransportError": "mcp_transport_error",
    "McpConnectionError": "mcp_transport_error",
    "McpSessionError": "mcp_transport_error",
    "McpToolError": "mcp_tool_error",
    "McpError": "mcp_tool_error",
}

_MODEL_ERROR_NAMES: frozenset[str] = frozenset(
    {
        # openai SDK / langchain-openai（OpenAI 兼容网关口径）
        "OpenAIError",
        "APIError",
        "APIStatusError",
        "APIResponseValidationError",
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "APITimeoutError",
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
        "ConflictError",
        "UnprocessableEntityError",
        "InternalServerError",
        # langchain 包装层
        "OpenAIRefusalError",
        "ModelError",
    }
)

_CONTEXT_OVERFLOW_NAMES: frozenset[str] = frozenset(
    {"ContextWindowExceededError", "ContextOverflowError", "ContextLengthExceededError"}
)
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "maximum context length",
    "context_length_exceeded",
    "context length is",
    "reduce the length of the messages",
    "too many tokens",
)

_TOOL_FALLBACK_MESSAGES: dict[str, str] = {
    "timeout": "工具执行超时",
    "sandbox_unavailable": "沙箱不可用",
    "param_invalid": "工具参数校验失败",
    "not_found": "目标文件或资源不存在",
    "permission_denied": "权限被拒绝",
    "mcp_transport_error": "MCP 连接失败",
    "mcp_tool_error": "MCP 工具返回错误",
    "cancelled": "工具调用已被取消",
    "internal": "工具执行失败",
}

_RUN_FALLBACK_MESSAGES: dict[str, str] = {
    "model_error": "模型调用失败",
    "rate_limited": "模型网关限流",
    "context_window_exceeded": "上下文窗口超限",
    "cancelled_by_user": "运行已被用户停止",
    "internal": "运行失败",
}

_MAX_MESSAGE_CHARS = 300  # wire 消息上限（防超长正文塞进事件库/前端卡片）

_TOOL_TYPE_RULES: tuple[tuple[type[BaseException], str], ...] = (
    (asyncio.CancelledError, "cancelled"),
    (TimeoutError, "timeout"),  # 3.11 起 asyncio.TimeoutError / socket.timeout 均为其别名
    (FileNotFoundError, "not_found"),
    (NotADirectoryError, "not_found"),
    (PermissionError, "permission_denied"),
    (ValueError, "param_invalid"),  # 含 pydantic ValidationError（子类）
    (TypeError, "param_invalid"),
)

_registered_tool_rules: dict[type[BaseException], str] = {}
_registered_run_rules: dict[type[BaseException], str] = {}


# ---------------------------------------------------------------- 注册接缝


def register_tool_error(exception_type: type[BaseException], code: str | None) -> None:
    """登记（或 `code=None` 注销）第一方异常 → ToolErrorCode 的映射。

    由异常**所在模块**在导入期调用（如 `app/sandbox/base.py` 注册 `SandboxUnavailable`）；
    注册项优先于类名表与类型规则——所以请注册最具体的类型（注册基类会让同族子类一起命中）。
    """
    if code is None:
        _registered_tool_rules.pop(exception_type, None)
        return
    if code not in TOOL_ERROR_CODES:
        raise ValueError(f"未知 ToolErrorCode：{code!r}（契约值域见 lcode_contracts.errors）")
    _registered_tool_rules[exception_type] = code


def register_run_error(exception_type: type[BaseException], code: str | None) -> None:
    """登记（或 `code=None` 注销）第一方异常 → RunErrorCode 的映射（语义同上一函数）。"""
    if code is None:
        _registered_run_rules.pop(exception_type, None)
        return
    if code not in RUN_ERROR_CODES:
        raise ValueError(f"未知 RunErrorCode：{code!r}（契约值域见 lcode_contracts.errors）")
    _registered_run_rules[exception_type] = code


# ---------------------------------------------------------------- 内部判定


def _registered_code(exc: BaseException, registry: Mapping[type[BaseException], str]) -> str | None:
    for exception_type, code in registry.items():
        if isinstance(exc, exception_type):
            return code
    return None


def _type_name(exc: BaseException | None) -> str:
    return type(exc).__name__ if exc is not None else ""


def _looks_like_context_overflow(exc: BaseException) -> bool:
    if type(exc).__name__ in _CONTEXT_OVERFLOW_NAMES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def tool_error_code(exc: BaseException | None) -> str:
    """异常 → ToolErrorCode（未知归 `internal`；不抛异常，分类器必须总给得出答案）。"""
    if exc is None:
        return "internal"
    registered = _registered_code(exc, _registered_tool_rules)
    if registered is not None:
        return registered
    name_rule = _TOOL_NAME_RULES.get(_type_name(exc))
    if name_rule is not None:
        return name_rule
    for exception_type, code in _TOOL_TYPE_RULES:
        if isinstance(exc, exception_type):
            return code
    return "internal"


def run_error_code(exc: BaseException | None) -> str:
    """异常 → RunErrorCode（限流 / 上下文超限 / 模型网关失败 / 取消 / 未分类）。"""
    if exc is None:
        return "internal"
    registered = _registered_code(exc, _registered_run_rules)
    if registered is not None:
        return registered
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled_by_user"
    name = _type_name(exc)
    if name == "RateLimitError":
        return "rate_limited"
    if _looks_like_context_overflow(exc):
        return "context_window_exceeded"
    if name in _MODEL_ERROR_NAMES:
        return "model_error"
    return "internal"


def retryable_tool(code: str, *, idempotent: bool | None) -> bool:
    """工具错误是否可重试：瞬态错误码 ∧ 工具非「明确不幂等」。

    `idempotent=None`（工具不在白名单也未显式否定）按**可重试**处理——ADR-002 的重试
    中间件本就只对幂等白名单放行，这里给出的是「错误侧建议」而非执行授权。
    """
    return code in TRANSIENT_TOOL_CODES and idempotent is not False


def retryable_run(code: str, exc: BaseException | None = None) -> bool:
    """运行错误是否可重试：限流恒 True；模型网关失败按异常是否瞬态类（或 5xx）判定。"""
    if code in _TRANSIENT_RUN_CODES:
        return True
    if code != "model_error":
        return False
    if _type_name(exc) in _TRANSIENT_MODEL_ERROR_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


def _safe_message(base: str, exc: BaseException | None, message: str | None) -> str:
    """构造 wire 安全的 message：显式传入优先（截断），否则固定文案 + 异常类名。"""
    if message:
        return message[:_MAX_MESSAGE_CHARS]
    name = _type_name(exc)
    text = f"{base}（{name}）" if name else base
    return text[:_MAX_MESSAGE_CHARS]


# ---------------------------------------------------------------- 对外分类入口


def classify_tool_error(
    exc: BaseException | None = None,
    *,
    message: str | None = None,
    idempotent: bool | None = None,
) -> ToolErrorObj:
    """异常 → `ToolErrorObj`（`tool_call_finished.error` 的载荷形态）。

    - `message`：不传则用固定文案 + 异常类名（NFR-004：不泄漏 SDK 原始文本）；
    - `idempotent`：该工具是否幂等（`None` = 未知，按可重试处理，见 `retryable_tool`）。
    """
    code = tool_error_code(exc)
    return ToolErrorObj(
        code=code,  # type: ignore[arg-type]  # code 由本模块保证取自契约值域
        message=_safe_message(_TOOL_FALLBACK_MESSAGES[code], exc, message),
        retryable=retryable_tool(code, idempotent=idempotent),
    )


def classify_tool_text(text: str, *, idempotent: bool | None = None) -> ToolErrorObj:
    """ToolMessage 错误正文 → `ToolErrorObj`（异常对象已丢失的收口路径）。

    识别 deepagents 0.6.12 的 execute 错误文案（`middleware/filesystem.py` 实测三型）：
    `Error: Execution not available.` / `Error: Invalid parameter.` / `Error: timeout ...`；
    其余归 `internal` 并把正文（已面向模型、非 SDK 原文）作为 message 截断保留。
    """
    lowered = text.lstrip().lower()
    if lowered.startswith("error: execution not available"):
        code = "sandbox_unavailable"
    elif lowered.startswith("error: invalid parameter"):
        code = "param_invalid"
    elif lowered.startswith("error: timeout"):
        code = "timeout"
    else:
        code = "internal"
    return ToolErrorObj(
        code=code,  # type: ignore[arg-type]
        message=_safe_message(_TOOL_FALLBACK_MESSAGES[code], None, text),
        retryable=retryable_tool(code, idempotent=idempotent),
    )


def classify_run_error(
    exc: BaseException | None = None, *, message: str | None = None
) -> RunErrorObj:
    """异常 → `RunErrorObj`（`run_status(phase=error).error` 的载荷形态）。"""
    code = run_error_code(exc)
    return RunErrorObj(
        code=code,  # type: ignore[arg-type]
        message=_safe_message(_RUN_FALLBACK_MESSAGES[code], exc, message),
        retryable=retryable_run(code, exc),
    )
