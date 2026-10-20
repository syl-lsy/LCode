"""unit：错误分类器（A3 §8.1 事件侧两个命名空间；test-plan §2.3/§5 T5）。

判据来源：
- `contracts/src/lcode_contracts/errors.py`：`ToolErrorCode`(9 值) / `RunErrorCode`(5 值)
  两个注册表互不混用，事件侧不进 REST 的 ERROR_MAP；
- A3 §8.1 末段：`retryable` 由「错误分类 + 工具幂等性」共同决定（NFR-009 口径）；
- NFR-004：SDK 原始异常文本（常含 key 片段/endpoint/prompt）不得进 wire —— 分类出的
  `message` 只用固定文案 + 异常类名，绝不 `str(exc)`。

本文件用「类名注册表 + 第一方 register() 挂钩」两条路径覆盖 sandbox/MCP 这类
本包不可 import 的异常（见 error_classifier 模块 docstring）。
"""

import asyncio
from typing import get_args

import pytest

from app.utils.error_classifier import (
    RUN_ERROR_CODES,
    TOOL_ERROR_CODES,
    TRANSIENT_TOOL_CODES,
    classify_run_error,
    classify_tool_error,
    classify_tool_text,
    register_run_error,
    register_tool_error,
    retryable_run,
    retryable_tool,
    run_error_code,
    tool_error_code,
)
from lcode_contracts.errors import RunErrorCode, ToolErrorCode


class SandboxUnavailableError(Exception):
    """模拟第一方领域异常：sandbox 车道落地后由其模块 register 登记（本包不 import 它）。"""


class McpTransportError(Exception):
    """模拟 MCP 传输层异常（同上：只按类名/注册登记，不做跨包 import）。"""


# ---------------------------------------------------------------- 注册表与契约同步


def test_code_sets_are_contract_subsets() -> None:
    """分类器产出的码值集合必须与契约 Literal 一致（契约改了三注册表，这里立刻红）。"""
    assert set(get_args(ToolErrorCode)) == TOOL_ERROR_CODES
    assert set(get_args(RunErrorCode)) == RUN_ERROR_CODES
    assert TRANSIENT_TOOL_CODES <= TOOL_ERROR_CODES


# ---------------------------------------------------------------- ToolErrorCode 分类


def test_cancelled_error_is_cancelled_and_not_retryable() -> None:
    obj = classify_tool_error(asyncio.CancelledError())
    assert obj.code == "cancelled"
    assert obj.retryable is False


@pytest.mark.parametrize("exc", [TimeoutError("boom"), TimeoutError(), TimeoutError("t")])
def test_timeout_variants_map_to_timeout(exc: BaseException) -> None:
    assert tool_error_code(exc) == "timeout"
    obj = classify_tool_error(exc, idempotent=True)
    assert obj.code == "timeout"
    assert obj.retryable is True  # 超时属瞬态，幂等工具可重试


def test_file_not_found_and_permission_denied() -> None:
    assert tool_error_code(FileNotFoundError("nope.py")) == "not_found"
    assert tool_error_code(PermissionError("denied")) == "permission_denied"
    assert classify_tool_error(PermissionError("denied")).retryable is False


def test_param_invalid_from_value_and_type_errors() -> None:
    assert tool_error_code(ValueError("bad arg")) == "param_invalid"
    assert tool_error_code(TypeError("bad type")) == "param_invalid"


def test_registered_first_party_exception_wins() -> None:
    register_tool_error(SandboxUnavailableError, "sandbox_unavailable")
    try:
        assert tool_error_code(SandboxUnavailableError("gone")) == "sandbox_unavailable"
        # 子类同样命中（isinstance 语义）
        obj = classify_tool_error(SandboxUnavailableError("gone"), idempotent=True)
        assert obj.code == "sandbox_unavailable"
        assert obj.retryable is True
    finally:
        register_tool_error(SandboxUnavailableError, None)


def test_sandbox_exception_detected_by_class_name_without_import() -> None:
    """跨包异常（e2b/OpenSandbox 不在本包 import 面）按类名兜底识别。"""

    class SandboxException(Exception):  # noqa: D401 —— 模拟 e2b.exceptions.SandboxException
        pass

    assert tool_error_code(SandboxException("crash")) == "sandbox_unavailable"


def test_mcp_exception_names_split_transport_vs_tool_error() -> None:
    register_tool_error(McpTransportError, "mcp_transport_error")
    try:
        assert tool_error_code(McpTransportError("session closed")) == "mcp_transport_error"
    finally:
        register_tool_error(McpTransportError, None)

    class McpToolError(Exception):
        pass

    assert tool_error_code(McpToolError("server returned isError")) == "mcp_tool_error"


def test_unknown_exception_falls_back_to_internal_without_leaking_text() -> None:
    secret = "sk-SECRET-abcdef http://gw.internal/v1"
    obj = classify_tool_error(RuntimeError(secret))

    assert obj.code == "internal"
    assert obj.retryable is False
    assert secret not in obj.message
    assert "RuntimeError" in obj.message  # 只暴露异常类名（便于排障，不含内容）


def test_explicit_message_is_used_for_tool_error() -> None:
    obj = classify_tool_error(None, message="命令不存在：git")
    assert obj.code == "internal"
    assert obj.message == "命令不存在：git"


@pytest.mark.parametrize(
    ("idempotent", "expected"),
    [(True, True), (None, True), (False, False)],
)
def test_retryable_matrix_combines_transience_with_idempotency(
    idempotent: bool | None, expected: bool
) -> None:
    assert retryable_tool("timeout", idempotent=idempotent) is expected
    assert retryable_tool("not_found", idempotent=idempotent) is False


# ---------------------------------------------------------------- ToolMessage 文本分类


def test_classify_tool_text_uses_deepagents_markers() -> None:
    """deepagents 0.6.12 的 execute 错误文本（middleware/filesystem.py:1738-1780 实测）。"""
    assert (
        classify_tool_text("Error: Execution not available. sandbox down").code
        == "sandbox_unavailable"
    )
    assert classify_tool_text("Error: Invalid parameter. command is required").code == (
        "param_invalid"
    )
    assert classify_tool_text("Error: timeout must be non-negative, got -1.").code == "timeout"
    obj = classify_tool_text("Error: something unexpected happened")
    assert obj.code == "internal"
    assert obj.retryable is False


# ---------------------------------------------------------------- RunErrorCode 分类


def test_run_cancelled_by_user() -> None:
    obj = classify_run_error(asyncio.CancelledError())
    assert obj.code == "cancelled_by_user"
    assert obj.retryable is False


def test_run_rate_limited_is_retryable() -> None:
    class RateLimitError(Exception):  # 模拟 openai.RateLimitError（按类名识别，不 import openai）
        pass

    obj = classify_run_error(RateLimitError("429"))
    assert obj.code == "rate_limited"
    assert obj.retryable is True


@pytest.mark.parametrize(
    ("name", "expected_retryable"),
    [
        ("APIConnectionError", True),
        ("APITimeoutError", True),
        ("InternalServerError", True),
        ("AuthenticationError", False),
        ("BadRequestError", False),
    ],
)
def test_run_model_errors_split_by_transience(name: str, expected_retryable: bool) -> None:
    exc = type(name, (Exception,), {})("boom")
    obj = classify_run_error(exc)
    assert obj.code == "model_error"
    assert obj.retryable is expected_retryable


def test_run_context_window_exceeded() -> None:
    class BadRequestError(Exception):
        pass

    exc = BadRequestError(
        "This model's maximum context length is 131072 tokens, however you requested 200000"
    )
    obj = classify_run_error(exc)
    assert obj.code == "context_window_exceeded"
    assert obj.retryable is False


def test_run_unknown_error_is_internal_and_hides_text() -> None:
    secret = "Bearer sk-SECRET-xyz"
    obj = classify_run_error(ValueError(secret))
    assert obj.code == "internal"
    assert obj.retryable is False
    assert secret not in obj.message


def test_run_error_registration_hook() -> None:
    class LcodeRunTimeout(Exception):
        pass

    register_run_error(LcodeRunTimeout, "model_error")
    try:
        assert run_error_code(LcodeRunTimeout()) == "model_error"
    finally:
        register_run_error(LcodeRunTimeout, None)


def test_retryable_run_matrix() -> None:
    assert retryable_run("rate_limited") is True
    assert retryable_run("cancelled_by_user") is False
    assert retryable_run("context_window_exceeded") is False
    assert retryable_run("internal") is False
