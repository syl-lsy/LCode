"""业务异常基类：所有领域异常的公共父类。

三个字段的职责边界（统一处理器 handler/handlers.py 只把 message 放进响应体）：
- message：面向用户展示的信息（如「商品不存在」），进响应体 message 字段；
- detail：面向开发者的补充说明（如 SQL/上下文），只进日志，绝不进响应体；
- original_exception：保留原始异常（数据库/SDK 抛出的那个）供根因追溯，只进日志。
"""


class BusinessException(Exception):
    """业务异常基类。

    `message` 面向用户（唯一进响应体的字段）；`detail` 面向开发者日志排查；
    `original_exception` 保留原始异常（数据库/SDK 抛出者）供根因追溯。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        original_exception: Exception | None = None,
    ):
        self.message = message
        self.detail = detail
        self.original_exception = original_exception
        super().__init__(message)
