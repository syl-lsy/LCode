"""通用域异常（A3 §8.1）：不归属具体资源的语义级校验失败。"""

from app.exception.base import BusinessException


class InvalidArgument(BusinessException):
    """语义级非法入参：pydantic 结构校验拦不住的场景（如枚举外值的业务判定，422）。"""


class MessageEmpty(BusinessException):
    """空白消息（AC-001-4，422）。

    注意落点：`MessageCreate` 的 validator 是二道防线（其失败会落 INVALID_ARGUMENT），
    为保证契约错误码区分度，**空白判定在 message_service 首位显式前置检查**
    （A3 §8.1 / §10 任务 7）。
    """
