"""运行域异常（A3 §8.1）：run 生命周期端点的三个错误路径。"""

from app.exception.base import BusinessException


class RunNotFound(BusinessException):
    """运行不存在：对未知 run 下 stop（404）。"""


class RunNotActive(BusinessException):
    """运行非活动态：对非运行中的 run 下 stop（409）。"""


class RunAlreadyActive(BusinessException):
    """会话已有活动 run：同会话再发消息（P0 会话级串行，409）。"""
