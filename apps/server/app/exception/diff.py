"""差异审查域异常（A3 §8.1）。"""

from app.exception.base import BusinessException


class DiffReviewNotFound(BusinessException):
    """差异审查不存在：diff 详情/文件/裁决端点（404）。"""


class DiffAlreadyResolved(BusinessException):
    """差异审查已裁决：重复裁决保护（409）。"""


class DiffFileNotFound(BusinessException):
    """文件不在该轮 diff 中：映射契约兜底码 `NOT_FOUND`（404，区别于上两者）。"""
