"""请求日志中间件：每请求生成 request_id + 创建 RequestLog（落到 request.state）。

- `request_id_var.set(uuid)`：该请求链路上的所有日志（业务/慢查询/异常）自动带上
  同一 id——contextvars 的写入在 `call_next` 之前完成，下游任务拿到的是含该值的上下文副本；
- `request.state.request_log = log`：全局异常处理器取它按级落异常日志——成功路径只在
  本中间件 `log.success()`，**每个请求恰好一条请求日志**（成功走中间件、异常走处理器）；
- 挂载位置：response 信封（最外层）之内、process_time 之外——信封要最后拿到响应，
  本中间件需要在 `call_next` 返回后立即记，并读到 process_time 写好的响应。
"""

import uuid
from typing import Any

from fastapi import Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.logger.log_record import request_id_var
from app.core.logger.request_log import RequestLog


async def logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id_var.set(str(uuid.uuid4()))

    log = RequestLog(
        message="请求完成",
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else "unknown",
    )
    request.state.request_log = log  # 供异常处理器等处按级补记

    response = await call_next(request)
    if not getattr(request.state, "exception_handled", False):
        # 异常响应（处理器已置标志）经同一个 request_log 按级记过，不再重复记 SUCCESS；
        # 成功路径在这里补齐真实状态码（request_log 构造时还拿不到响应）
        log.status_code = response.status_code
        log.success()
    return response


MIDDLEWARE: tuple[Any, dict[str, Any]] = (logging_middleware, {})
