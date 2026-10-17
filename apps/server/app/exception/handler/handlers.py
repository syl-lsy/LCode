"""FastAPI 全局异常处理器：请求-响应链路所有异常的最终出口。

由 `main.py` 注册四处，覆盖四类异常：

- `RequestValidationError`：请求参数校验失败（INVALID_ARGUMENT/422，message 拼
  `loc: msg` 明细）；
- starlette `HTTPException`：框架层协议错误（404/405/415 走 `PROTOCOL_ERROR_MAP`
  友好消息，其余状态码回退 `exc.detail`；code 为 HTTP 状态码字符串）；
- `BusinessException`：领域异常（message 面向用户进响应体；detail / original_exception
  只进日志）；
- `Exception`：未知异常兜底（INTERNAL/500，用默认消息，不泄漏内部细节）。

响应统一为 `{"code": <契约 RestErrorCode>, "message": <面向用户的信息>}`（NFR-002 /
契约 `errors.ErrorBody`）——**本处理器只负责错误侧响应**；成功侧 `/api/` 信封见
`app/core/middleware/response.py`（两侧是同一份响应契约，基座铁律 9）。
"""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.exception.base import BusinessException
from app.exception.handler.errors import ERROR_MAP, PROTOCOL_ERROR_MAP

logger = logging.getLogger(__name__)


def _lookup(exc: BaseException) -> tuple[str, int, str]:
    """按 MRO 查找异常对应的错误码配置，找不到则回退到 Exception 兜底条目。

    子类未注册时自动落最近已注册父类——业务代码不必为每个细分异常都登记，
    但真正的领域异常必须在 `ERROR_MAP` 显式注册（防漂移断言见单测）。
    """
    for exc_type in type(exc).__mro__:
        if exc_type in ERROR_MAP:
            return ERROR_MAP[exc_type]
    return ERROR_MAP[Exception]


def _format_validation_error(err: dict) -> str:
    """校验错误明细：`query.n: Input should be a valid integer`（loc 用点连接）。"""
    loc = ".".join(str(p) for p in err["loc"])
    return f"{loc}: {err['msg']}"


async def exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 标记错误响应已终态（{code, message}）：统一信封中间件据此跳过，
    # 保证错误响应不被成功信封二次包装（两个中间件共享同一 scope 的 state）。
    request.state.exception_handled = True

    if isinstance(exc, HTTPException):
        code = str(exc.status_code)
        http_status = exc.status_code
        # 协议表未收录的状态码回退 detail；裸抛 HTTPException(429) 时 detail 可能为 None，归一为空串
        body_msg = PROTOCOL_ERROR_MAP.get(exc.status_code) or (
            exc.detail if isinstance(exc.detail, str) and exc.detail else ""
        )
    else:
        code, http_status, default_msg = _lookup(exc)
        if isinstance(exc, RequestValidationError):
            body_msg = "\n".join(_format_validation_error(e) for e in exc.errors())
        elif isinstance(exc, BusinessException):
            body_msg = exc.message
            # 业务异常 = 预期控制流走 warning；仅包装了原始异常（基础设施故障）时才带栈
            logger.warning(
                "业务异常: %s | detail=%s",
                exc.message,
                exc.detail,
                exc_info=exc.original_exception,
            )
        else:
            body_msg = default_msg
            logger.error("未处理异常: %s", exc, exc_info=exc)

    return JSONResponse(status_code=http_status, content={"code": code, "message": body_msg})
