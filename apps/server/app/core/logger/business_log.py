"""业务日志：`@service_logger` 装饰器统一处理业务操作的成败日志（不必每处手写）。

语义与全局异常处理器保持一致（基座铁律 11）——**业务异常是预期控制流走 warning
（无栈），未知异常才 error 带栈**；id_extractor 提取操作对象 id（提取失败静默忽略，
日志绝不因它报错）。LCode 落点：M2 的 service 层（会话/运行/审批等关键操作）挂本
装饰器，`agent/` 运行时按需手写 `BusinessLog(...)`。
"""

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from app.core.logger.log_record import LogRecord
from app.exception.base import BusinessException


@dataclass
class BusinessLog(LogRecord):
    """业务级差异字段：action（操作）/ entity（操作对象类型）/ entity_id（对象 id）。"""

    action: str = ""
    entity: str = ""
    entity_id: str | int = ""


# 提取操作对象 id：收到 (位置参数, 关键字参数, 返回值/失败时为 None)
EntityIdExtractor = Callable[[tuple[Any, ...], dict[str, Any], Any], str | int]


def service_logger(
    action: str,
    entity: str = "",
    id_extractor: EntityIdExtractor | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """业务操作日志装饰器：成功 success / 业务异常 warning / 其他异常 error（带栈）。

    用法（service 层方法上，M2 示例）：

        @service_logger("创建会话", entity="session", id_extractor=lambda a, k, r: r.id)
        async def create_session(self, data: SessionCreate) -> SessionResponse: ...
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            log = BusinessLog(
                message="",
                action=action,
                entity=entity,
            )
            try:
                result = await func(*args, **kwargs)
                if id_extractor:
                    with contextlib.suppress(Exception):  # 提取失败静默忽略，不影响业务结果
                        log.entity_id = id_extractor(args, kwargs, result)
                log.message = f"{action}成功"
                log.success()
                return result
            except Exception as e:
                # 失败路径也尝试提取 id（result 传 None）：有的实现能从入参拿到对象 id，
                # 拿到就带上，拿不到/抛错都静默忽略
                if id_extractor:
                    with contextlib.suppress(Exception):
                        log.entity_id = id_extractor(args, kwargs, None)
                log.message = f"{action}失败"
                if isinstance(e, BusinessException):
                    log.warning()
                else:
                    log.error(exc=e)
                raise

        return wrapper

    return decorator
