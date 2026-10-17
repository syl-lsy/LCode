"""FastAPI 入口（A3 §1.1）：中间件注册表 + 四处异常注册 + 双健康探针 + lifespan。

启动顺序（不可调换）：
1. `register_middleware(app)`——`core/middleware` 注册表（含 `/api/` 成功信封）；
2. `setup_openapi(app)`——OpenAPI 信封同步（与响应中间件同契约，两侧一起动）；
3. 四处异常注册——校验失败 / 协议错误 / 领域异常 / 未知异常兜底；
4. 路由挂载——`api/v1` 聚合点（M2 逐个 include，M1 为空结构）。

lifespan 槽位：M2 在此启停沙箱管理器与空闲计时（A3 §4.3）；当前只释放引擎连接池。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from starlette.exceptions import HTTPException

from app.api.v1 import api_router
from app.core.config import common_settings, web_settings
from app.core.db import dispose_engine, get_engine
from app.core.middleware import register_middleware
from app.core.openapi import setup_openapi
from app.exception import BusinessException
from app.exception.handler import exception_handler, generate_error_docs

# 生产环境隐藏文档入口（/docs、/redoc、/openapi.json 全部关闭）
_hide_docs = common_settings.environment == "production"

# LOG_LEVEL（NFR-007）落到 root logger：uvicorn 自带 access 日志，应用日志走根层级
logging.basicConfig(level=common_settings.log_level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动期装载 / 关闭期释放。

    M2 挂点（A3 §4.3）：启动时装配 `SandboxManager`（会话沙箱注册表 + 空闲 pause
    计时任务），关闭时对 running 沙箱显式 `pause()` 保留状态（硬约束 8：模板自动
    休眠不保状态），再停计时任务。当前只做「关闭时释放数据库连接池」。
    """
    yield
    await dispose_engine()


app = FastAPI(
    title=web_settings.app_name,
    version="0.1.0",
    # /docs 的「错误码说明」由 ERROR_MAP 自动生成（契约码字符串）
    description=generate_error_docs(),
    docs_url=None if _hide_docs else "/docs",
    redoc_url=None if _hide_docs else "/redoc",
    openapi_url=None if _hide_docs else "/openapi.json",
    lifespan=lifespan,
)

# 中间件统一在 app/core/middleware/__init__.py 的注册表声明（当前含 X-Process-Time
# 与 /api/ 统一成功信封），须在异常处理器注册之前、FastAPI 实例化之后挂载。
register_middleware(app)

# OpenAPI 同步：/api/ 路径的 200/201 响应 schema 包装为统一信封，与响应中间件保持一致。
# 在文档首次生成前调用即可——openapi 生成是惰性的（monkey-patch app.openapi）。
setup_openapi(app)

# 全局异常处理四处注册：校验失败 / 协议错误 / 领域异常 / 未知异常兜底。
# BusinessException 必须显式注册——starlette 把 Exception 注册路由给最外层
# ServerErrorMiddleware，它构造响应后仍会 re-raise（供服务器记日志）：业务异常
# 若不显式注册就会在 ExceptionMiddleware 层冒泡走这条路，每个业务 404/409 都
# 产生 ERROR traceback 噪音，且下游默认 TestClient(app)
# （raise_server_exceptions=True）拿到的是异常而不是统一 JSON。
app.add_exception_handler(RequestValidationError, exception_handler)
app.add_exception_handler(HTTPException, exception_handler)
app.add_exception_handler(BusinessException, exception_handler)
app.add_exception_handler(Exception, exception_handler)

# 路由挂载：v1 聚合点（前缀 /api/v1）；SSE 端点须自设 is_stream 跳过成功信封。
app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探针（Liveness Probe）：只证明进程能响应请求，不依赖外部服务。

    K8s 每 30 秒调用一次，连续失败 3 次判定 Pod 死亡并重启；
    因此这里绝不能加数据库/外部服务检查——依赖抖动会误杀健康的 Pod。
    """
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    """就绪探针（Readiness Probe）：检查数据库连通性（SELECT 1），不抛异常。

    K8s 每 10 秒调用一次，连续失败 3 次将该 Pod 从 Service 转发列表摘除
    （不重启，仅不接收新流量），待恢复后自动重新加入。
    探测失败用 200 + 状态字段表达（而非 5xx）：探针语义是「答未就绪」，
    不是「接口出错」，避免把编排系统与监控系统引入误判。
    """
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception:
        return {"status": "not ready", "database": "disconnected"}
