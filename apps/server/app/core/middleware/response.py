"""统一成功信封中间件：`/api/` 路径的成功响应包装为 `{code, data, message}`。

契约口径（`contracts/rest.py` 模块 docstring / README §六；A3 §1.3 铁律 9 本地化）：

- **成功侧**：`{"code": "0", "data": <DTO>, "message": "ok"}`，`code` 取契约
  `SUCCESS_CODE`（字符串 `"0"`，非数字）；仅 `/api/` 前缀路径包装
  （`/health`、`/docs` 等运维/文档端点保持原样）；
- **错误侧**：异常处理器已输出 `{code, message}` 终态（并置 `request.state.exception_handled`），
  本中间件据该标志跳过，绝不二次包装；
- **204 与 SSE 不包装**：SSE 等流式端点必须自设 `request.state.is_stream = True`
  （否则本中间件消费 `body_iterator` 会把流式响应拼成整块 JSON），或把路由放在
  `/api/` 外；204/304/1xx 与 HEAD 由状态码与请求方法判定跳过；
- **非 JSON 响应**（PlainTextResponse / FileResponse 等）不要挂 `/api/` 前缀：
  响应体会在 `json.loads` 处解析失败被兜底转成 INTERNAL——这类端点同样须自设
  `is_stream` 或移出 `/api/` 前缀。

包装时重建 `JSONResponse`：status_code 沿用原响应，响应头逐条从原 `raw_headers`
搬运（剔除 content-length，新长度由信封体重算）——**不能用 `dict(headers)` 折叠**，
否则多值头（多个 Set-Cookie）会静默丢失。

成功与错误两侧是同一份响应契约：改信封格式必须同时改本文件与 `app/core/openapi.py`
（OpenAPI 成功响应 schema 同步），两侧一起动（基座铁律 9）。
"""

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

_NO_BODY_STATUS = frozenset(range(100, 200)) | {204, 304}


async def unified_response(request: Request, call_next: RequestResponseEndpoint) -> Response:
    response = await call_next(request)

    if (
        not request.url.path.startswith("/api/")
        or getattr(request.state, "exception_handled", False)
        or getattr(request.state, "is_stream", False)
        or request.method == "HEAD"
        or response.status_code in _NO_BODY_STATUS
    ):
        return response

    body = b""
    # starlette 的类型存根未在基类 Response 上声明 body_iterator（运行期存在：
    # 非流式响应也由 body_iterator 提供 body），故此处显式忽略属性检查
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        body += chunk

    data = json.loads(body) if body else None

    new_response = JSONResponse(
        content={"code": "0", "data": data, "message": "ok"},
        status_code=response.status_code,
    )
    # 逐条搬运原响应头（保留重复头，如多个 Set-Cookie）；content-length 剔除后
    # 只保留新响应自带的那条（信封体长度与原响应体不同）
    kept_headers = [
        (name, value) for name, value in response.raw_headers if name.lower() != b"content-length"
    ]
    new_response.raw_headers = kept_headers + [
        (name, value) for name, value in new_response.raw_headers if name == b"content-length"
    ]
    return new_response


MIDDLEWARE: tuple[Any, dict[str, Any]] = (unified_response, {})
