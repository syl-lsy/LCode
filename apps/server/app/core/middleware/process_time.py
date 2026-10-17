"""请求耗时中间件：每个响应附 `X-Process-Time` 头（毫秒，perf_counter 口径）。"""

import time
from typing import Any

from fastapi import Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response


async def process_time(request: Request, call_next: RequestResponseEndpoint) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(round((time.perf_counter() - start) * 1000, 2))
    return response


MIDDLEWARE: tuple[Any, dict[str, Any]] = (process_time, {})
