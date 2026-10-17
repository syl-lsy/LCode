"""错误码注册表（唯一事实源）。

三个注册表互不混用：
- ToolErrorCode：tool_call_finished / browser_action 事件的 error.code（吸收
  design/research/tool-fault-tolerance.md §4.3 草案，M1 定稿）
- RunErrorCode：run_status 事件的 error.code（运行级错误）
- RestErrorCode：REST 错误体 {"code","message"} 的 code（成功恒为 "0"）

约束：事件侧错误码与 REST 错误码是两个命名空间，不要求互相覆盖。
"""

from typing import Literal

from .common import ContractModel

ToolErrorCode = Literal[
    "timeout",              # 工具执行超时（含 MCP wait_for 超时）
    "sandbox_unavailable",  # 沙箱不可用/崩溃（触发 AC-002-4 重建路径）
    "param_invalid",        # 参数校验失败（框架已转 error ToolMessage 回模型自纠）
    "not_found",            # 目标文件/资源不存在
    "permission_denied",    # 权限拒绝（含审批拒绝后落库的终态）
    "mcp_transport_error",  # MCP transport/会话失败（模型无法行动类）
    "mcp_tool_error",       # MCP 服务端 isError=True（模型可自纠类）
    "cancelled",            # 用户停止/运行取消
    "internal",             # 未分类内部错误（不泄漏服务端细节）
]

RunErrorCode = Literal[
    "model_error",             # 模型 provider 调用失败（含鉴权/参数类）
    "rate_limited",            # 限流
    "context_window_exceeded", # 上下文窗口超限且压缩兜底失败
    "cancelled_by_user",       # 用户点击停止（AC-001-2）
    "internal",
]

RestErrorCode = Literal[
    "INVALID_ARGUMENT",
    "MESSAGE_EMPTY",            # AC-001-4：空输入/纯空白
    "SESSION_NOT_FOUND",
    "RUN_NOT_FOUND",
    "RUN_NOT_ACTIVE",           # 对非运行中 run 下 stop
    "RUN_ALREADY_ACTIVE",       # 同会话已有活动 run 时再发消息（P0 串行）
    "APPROVAL_NOT_FOUND",
    "APPROVAL_ALREADY_RESOLVED",# 重复决策（AC-006-5 幂等保护）
    "DIFF_REVIEW_NOT_FOUND",
    "DIFF_ALREADY_RESOLVED",
    "MODEL_NOT_FOUND",
    "NOT_FOUND",                # 兜底 404
    "INTERNAL",
]

SUCCESS_CODE = "0"

# REST 错误码 -> HTTP 状态码映射（README 附完整表；此处机器可校验）
REST_HTTP_STATUS: dict[str, int] = {
    "INVALID_ARGUMENT": 422,
    "MESSAGE_EMPTY": 422,
    "SESSION_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "RUN_NOT_ACTIVE": 409,
    "RUN_ALREADY_ACTIVE": 409,
    "APPROVAL_NOT_FOUND": 404,
    "APPROVAL_ALREADY_RESOLVED": 409,
    "DIFF_REVIEW_NOT_FOUND": 404,
    "DIFF_ALREADY_RESOLVED": 409,
    "MODEL_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "INTERNAL": 500,
}


class ToolErrorObj(ContractModel):
    """事件侧工具错误对象（tool_call_finished / browser_action）。"""

    code: ToolErrorCode
    message: str
    retryable: bool | None = None  # 由错误分类 + 工具幂等性共同决定（NFR-009 口径）


class RunErrorObj(ContractModel):
    """事件侧运行错误对象（run_status phase=error）。"""

    code: RunErrorCode
    message: str
    retryable: bool | None = None


class ErrorBody(ContractModel):
    """REST 错误体（/api/ 错误侧统一信封；成功侧为 {"code":"0","data",...}）。"""

    code: RestErrorCode
    message: str
