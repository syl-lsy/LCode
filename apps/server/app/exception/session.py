"""会话域异常（`SESSION_NOT_FOUND`，A3 §8.1）。"""

from app.exception.base import BusinessException


class SessionNotFound(BusinessException):
    """会话不存在：会话详情/改删/消息/事件/成本/沙箱端点统一抛此异常（404）。"""
