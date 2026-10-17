"""领域异常统一出口：业务代码从这里 import（`from app.exception import SessionNotFound`）。

新增领域异常 = 本目录新增一个文件定义 `BusinessException` 子类 → 加进下面的 `__all__`
→ 在 `app/exception/handler/errors.py` 的 `ERROR_MAP` 注册（契约错误码 + 默认消息）；
错误码文档（/docs 的「错误码说明」）会随注册自动更新。单元测试
`test_exception_handlers.py::test_exported_domain_exceptions_are_registered_in_error_map`
对「导出但未注册」防漂移。

文件划分跟契约错误表族走（A3 §8.1）：session / run / approval / diff / model / common。
"""

from app.exception.approval import ApprovalAlreadyResolved, ApprovalNotFound
from app.exception.base import BusinessException
from app.exception.common import InvalidArgument, MessageEmpty
from app.exception.diff import DiffAlreadyResolved, DiffFileNotFound, DiffReviewNotFound
from app.exception.model import ModelNotFound
from app.exception.run import RunAlreadyActive, RunNotActive, RunNotFound
from app.exception.session import SessionNotFound

__all__ = [
    "ApprovalAlreadyResolved",
    "ApprovalNotFound",
    "BusinessException",
    "DiffAlreadyResolved",
    "DiffFileNotFound",
    "DiffReviewNotFound",
    "InvalidArgument",
    "MessageEmpty",
    "ModelNotFound",
    "RunAlreadyActive",
    "RunNotActive",
    "RunNotFound",
    "SessionNotFound",
]
