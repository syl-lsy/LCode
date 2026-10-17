"""审批域异常（A3 §8.1）。"""

from app.exception.base import BusinessException


class ApprovalNotFound(BusinessException):
    """审批不存在：审批详情/决策端点（404）。"""


class ApprovalAlreadyResolved(BusinessException):
    """审批已处理：重复决策的幂等保护（AC-006-5，409）。"""
