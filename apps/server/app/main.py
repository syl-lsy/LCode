"""FastAPI 入口（A3 §1.1）：日志单管线 + 中间件注册表 + 四处异常注册 + 双健康探针 + lifespan。

启动顺序（不可调换）：
0. `_silence_uvicorn_loggers()` + `setup_logging()`——日志系统最先就位（基座铁律 11），
   其余组件打印日志前完成配置；
1. `register_middleware(app)`——`core/middleware` 注册表（含请求日志与 `/api/` 成功信封）；
2. `setup_openapi(app)`——OpenAPI 信封同步（与响应中间件同契约，两侧一起动）；
3. 四处异常注册——校验失败 / 协议错误 / 领域异常 / 未知异常兜底；
4. 路由挂载——`api/v1` 聚合点（M2 逐个 include，M1 为空结构）。

lifespan 槽位（A3 §4.3）：启动期起 SandboxManager 的空闲 pause 清扫循环，关闭期对
running 沙箱显式 pause（保状态可续，硬约束 8）再停循环；EventPipeline 单例在导入期
装配完成（校验→落库→广播皆惰性），无启停动作，此处显式取用作为装配锚点。
"""

import asyncio
import contextlib
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
from app.core.logger import BusinessLog, setup_logging
from app.core.middleware import register_middleware
from app.core.openapi import setup_openapi
from app.core.postgres_checkpointer import (
    setup_postgres_checkpointer_if_requested,
    teardown_postgres_checkpointer,
)
from app.dependencies import (
    get_event_pipeline,
    get_sandbox_manager,
    set_main_loop,
)
from app.exception import BusinessException
from app.exception.handler import exception_handler, generate_error_docs
from app.sandbox.manager import SandboxManager

IDLE_SWEEP_INTERVAL_S: float = 30.0
"""空闲清扫周期（秒）：远小于 `SANDBOX_IDLE_PAUSE_SECONDS` 默认阈值（600s），保证
pause 的触发精度由 evaluate_idle 的阈值判定决定、不被清扫周期放大（AC-002-2）。"""


def _silence_uvicorn_loggers() -> None:
    """关闭 uvicorn 的 access/error 标准库 logger（幂等，可在多处重复调用）。

    日志单管线（基座铁律 11 / NFR-007）：请求日志由 logging 中间件统一记（每请求
    恰一条，`core/logger/` 配置）；ServerErrorMiddleware 对未知异常构造响应后
    re-raise 的兜底记录，也已由全局异常处理器经 request.state.request_log 按 ERROR
    带栈承担。保留 uvicorn 自己的输出只会与 loguru 管线重复（访问日志每请求多打
    一条）、且绕过 request_id 串联。
    """
    logging.getLogger("uvicorn.error").disabled = True
    logging.getLogger("uvicorn.access").disabled = True


# 日志系统在系统启动时完成配置（须在其余组件打印日志前）：
# development = 彩色控制台 + ERROR 额外落文件（LOG_FILE_DIR）；production = stdout 单行 JSON。
_silence_uvicorn_loggers()
setup_logging()

# 生产环境隐藏文档入口（/docs、/redoc、/openapi.json 全部关闭）
_hide_docs = common_settings.environment == "production"


async def idle_sweeper(manager: SandboxManager) -> None:
    """空闲清扫循环（A3 §4.3 / AC-002-2）：按 `IDLE_SWEEP_INTERVAL_S` 周期评估空闲沙箱。

    到阈值且无活跃 run 才 pause 的判定在 `SandboxManager.evaluate_idle`（Q3：run 活跃
    期间永不暂停）；本循环只负责节拍，单轮异常不终止循环（pause 失败已由 manager 记
    日志标 crashed）。
    """
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_S)
        try:
            manager.evaluate_idle()
        except Exception as exc:  # noqa: BLE001 —— 单轮清扫失败不终止循环
            BusinessLog(
                message="沙箱空闲清扫单轮失败（循环继续）",
                action="清扫空闲沙箱",
                entity="sandbox",
            ).warning(exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动期装载 / 关闭期释放。

    启动期先**再关一次** uvicorn 标准库 logger（幂等）：uvicorn 每次启动都会按自己
    的 log 配置重建标准库 logger（dictConfig 会把 disabled 重置为 False），而
    `fastapi dev` 在配置日志**之前**就先导入了本模块——模块顶部那次关闭会被随后的
    配置覆盖；启动钩子在服务真正起跑前再关一次，此后含访问日志在内的 uvicorn 输出
    都走 loguru 单管线（只剩 uvicorn 起跑前 CLI 打出的 Started server process /
    Waiting for application startup 两行属预期残留；`uvicorn app.main:app` 直启时连
    这两行也没有）。

    M2 挂点（A3 §4.3）：启动期起 `SandboxManager` 的空闲 pause 清扫循环（事件出口与
    计费分段回调已在 dependencies 装配期接好，manager 单例直接可用）；`EventPipeline`
    单例无启停动作，显式取用作为装配锚点（管线故障会在首个 emit 处暴露，不在此处）。
    e2e 车道（默认零副作用）：`LCODE_E2E_CHECKPOINTER=postgres` 时启动期装配真
    `AsyncPostgresSaver`（建连 + setup + 注入 dependencies，见 core/postgres_checkpointer），
    关闭期先于业务池释放其连接池。
    关闭期**先停清扫循环**再对 running 沙箱显式 `pause()` 保留状态（硬约束 8：模板
    自动休眠不保状态）——顺序不可调换，防止清扫循环与关停 pause 竞争同一相位机；最后
    释放数据库连接池。
    """
    _silence_uvicorn_loggers()
    manager = get_sandbox_manager()
    get_event_pipeline()  # 装配锚点：确认事件出口单例就位（无启停动作，见上）
    set_main_loop(asyncio.get_running_loop())  # 沙箱输出桥的线程→主循环投递目标（_launch）
    checkpointer_saver = await setup_postgres_checkpointer_if_requested()
    sweeper = asyncio.create_task(idle_sweeper(manager), name="lcode-sandbox-idle-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        with contextlib.suppress(Exception):
            manager.shutdown()  # 单会话 pause 失败已在 manager 内记账（crashed），不阻断关停
        with contextlib.suppress(Exception):
            await teardown_postgres_checkpointer(checkpointer_saver)
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

# 中间件统一在 app/core/middleware/__init__.py 的注册表声明（当前含 X-Process-Time、
# 请求日志与 /api/ 统一成功信封），须在异常处理器注册之前、FastAPI 实例化之后挂载。
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
