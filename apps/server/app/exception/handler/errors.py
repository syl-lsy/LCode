"""错误码表：异常类型 → (契约错误码, HTTP 状态码, 默认消息)。

与基座的差异（ADR-004 偏差 9）：`code` 值不是基座的数字码（404001 族），而是**契约
字符串码**（`RestErrorCode`，唯一事实源 `contracts/src/lcode_contracts/errors.py`）；
HTTP 状态码从契约的 `REST_HTTP_STATUS` 派生，本文件不二次手写映射——契约改映射时
这里自动跟随，把唯一可能漂移的环节杀掉（ADR-003 契约单源的落地姿势）。

- `ERROR_MAP` 是错误码的唯一事实源：新增领域异常必须在此注册，否则按 MRO 回退到
  最近已注册的父类（最终兜底 `Exception` → INTERNAL/500）；
- `PROTOCOL_ERROR_MAP` 覆盖框架层协议错误（路径/方法/媒体类型），其 code 用 HTTP
  状态码字符串（契约未给协议错误分配业务码）；
- 事件侧两个命名空间（`ToolErrorCode` 9 值 / `RunErrorCode` 5 值）**不进本表**——
  它们走 `run_status` / `tool_call_finished` 事件载荷，由 `utils/error_classifier.py`
  分类填充（A3 §8.1）。
"""

from fastapi.exceptions import RequestValidationError

from app.exception.approval import ApprovalAlreadyResolved, ApprovalNotFound
from app.exception.common import InvalidArgument, MessageEmpty
from app.exception.diff import DiffAlreadyResolved, DiffFileNotFound, DiffReviewNotFound
from app.exception.model import ModelNotFound
from app.exception.run import RunAlreadyActive, RunNotActive, RunNotFound
from app.exception.session import SessionNotFound
from lcode_contracts.errors import REST_HTTP_STATUS

# (code, HTTP, message) 三元组
_ErrorEntry = tuple[str, int, str]


def _entry(code: str, message: str) -> _ErrorEntry:
    """按契约码取 HTTP 状态码：key 不存在即 KeyError（启动期暴露拼错的码，不静默降级）。"""
    return (code, REST_HTTP_STATUS[code], message)


ERROR_MAP: dict[type[BaseException], _ErrorEntry] = {
    # 框架层：请求体/查询参数/路径参数校验失败（message 由处理器拼 loc 明细）
    RequestValidationError: _entry("INVALID_ARGUMENT", "数据验证错误"),
    # 领域异常（A3 §8.1 映射表逐条落地）
    SessionNotFound: _entry("SESSION_NOT_FOUND", "会话不存在"),
    RunNotFound: _entry("RUN_NOT_FOUND", "运行不存在"),
    RunNotActive: _entry("RUN_NOT_ACTIVE", "该运行已不在运行中"),
    RunAlreadyActive: _entry("RUN_ALREADY_ACTIVE", "该会话已有运行中的任务"),
    ApprovalNotFound: _entry("APPROVAL_NOT_FOUND", "审批不存在"),
    ApprovalAlreadyResolved: _entry("APPROVAL_ALREADY_RESOLVED", "该审批已处理"),
    DiffReviewNotFound: _entry("DIFF_REVIEW_NOT_FOUND", "差异审查不存在"),
    DiffAlreadyResolved: _entry("DIFF_ALREADY_RESOLVED", "该差异审查已裁决"),
    DiffFileNotFound: _entry("NOT_FOUND", "文件不在该轮 diff 中"),
    ModelNotFound: _entry("MODEL_NOT_FOUND", "模型不存在"),
    InvalidArgument: _entry("INVALID_ARGUMENT", "参数非法"),
    MessageEmpty: _entry("MESSAGE_EMPTY", "消息内容不能为空"),
    # 兜底：未注册异常一律 INTERNAL/500（detail 与堆栈只进日志）
    Exception: _entry("INTERNAL", "服务器内部错误"),
}

PROTOCOL_ERROR_MAP: dict[int, str] = {
    404: "路径不存在",
    405: "请求方法不允许",
    415: "不支持的媒体类型",
}
