"""中间件注册表：集中声明、统一挂载（main.py 只调 register_middleware(app)）。

约定：一个中间件一个文件，文件内导出 `MIDDLEWARE: tuple[Any, dict[str, Any]]`
（第一项是 ASGI 中间件类或 http 函数式中间件，第二项是传给它的关键字参数）。
**第二项 kwargs 只有类式中间件生效**（走 `app.add_middleware(Cls, **kwargs)`）；
函数式中间件经 `app.middleware("http")` 注册只收位置参数——配置要么写进闭包，
要么由模块级读 settings 常量（现有 process_time / response 即此模式），
给函数式中间件塞非空 kwargs 会直接 TypeError。
新增中间件 = 新建文件 + 把它加进下面的 MIDDLEWARES 列表。

注册顺序：starlette 每注册一个中间件都插到内部列表头部，因此 **MIDDLEWARES 里
越靠后的中间件越靠外**——最先收到请求、最后拿到响应。例如 response.MIDDLEWARE
放最后，才能读到内层 process_time 已写好的响应头；函数式与类式中间件按列表
顺序统一处理，不区分先后规则。
"""

import inspect
from typing import Any

from fastapi import FastAPI

from app.core.middleware import process_time, response

MIDDLEWARES: list[tuple[Any, dict[str, Any]]] = [
    process_time.MIDDLEWARE,
    response.MIDDLEWARE,
]


def register_middleware(app: FastAPI) -> None:
    """把 MIDDLEWARES 声明的中间件逐个挂到 app 上。

    类式中间件（如 CORSMiddleware）走 `app.add_middleware`；
    函数式中间件（形如 `async def mw(request, call_next)`）走 `app.middleware("http")`。
    """
    for callable_obj, kwargs in MIDDLEWARES:
        if inspect.isclass(callable_obj):
            app.add_middleware(callable_obj, **kwargs)  # type: ignore[arg-type]
        else:
            app.middleware("http")(callable_obj, **kwargs)
