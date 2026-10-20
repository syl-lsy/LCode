"""会话端点（契约前 5 端点；A3 §1.1 / §10 任务 7 会话车道）。

| 方法 | 路径 | 请求 → 响应 |
|---|---|---|
| POST   | `/api/v1/sessions` | `SessionCreate` → `SessionDetail`（201） |
| GET    | `/api/v1/sessions` | → `SessionPage`（最近活跃排序 + `?title=` 子串搜索，AC-004-3） |
| GET    | `/api/v1/sessions/{session_id}` | → `SessionDetail`（含 run_state，AC-006-5 刷新恢复） |
| PATCH  | `/api/v1/sessions/{session_id}` | `SessionPatch` → `SessionDetail`（档位热切换） |
| DELETE | `/api/v1/sessions/{session_id}` | → 204（同步 kill 沙箱 + 清业务行，AC-002-3） |

`DELETE` 的删除编排（停活动 run → kill 沙箱 → 清行）在 run_service（沙箱管理器持有者 +
编排唯一入口）；本文件只做路由与 204 出口（无响应体 → 无信封，response middleware 按状态码跳过）。

约定（基座铁律 3/8/9）：路由函数只做**路由 + 参数校验 + 依赖注入**，业务逻辑全在
`app/services/`；成功响应由 `/api/` 信封中间件统一包装
（`{"code":"0","data":...,"message":"ok"}`），错误一律走全局异常处理器出口（契约字符串码），
本文件不手拼错误 JSONResponse、不 import `app.models`（DTO ≠ ORM）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import ModelRegistryDep, RunServiceDep, get_db
from app.schemas import SessionCreate, SessionDetail, SessionPage, SessionPatch
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
"""请求级事务会话（基座铁律 10：service 只 flush，提交/回滚由 `get_db` 统一处理）。

本车道的局部别名：其余车道（messages/events）各自声明同构别名，避免为一个类型标注去动
共享的 `dependencies.py`。
"""


def get_session_service(db: DbDep, registry: ModelRegistryDep) -> SessionService:
    """装配会话 service（构造注入，基座 service-layer 口径）：本文件唯一的装配点。"""
    return SessionService(db, registry)


ServiceDep = Annotated[SessionService, Depends(get_session_service)]
"""路由层依赖锚点：路由函数只拿到 service，看不到会话/注册表。"""


@router.post("", response_model=SessionDetail, status_code=201, summary="创建会话")
async def create_session(payload: SessionCreate, service: ServiceDep) -> SessionDetail:
    """创建会话：`sandbox_provider` 固化当前 SANDBOX_PROVIDER（AC-002-6）。

    未知模型 → 404 MODEL_NOT_FOUND；档位与模型能力冲突 → 422 INVALID_ARGUMENT；
    载荷超出契约（含采样参数等未知字段）→ 422 INVALID_ARGUMENT（契约 DTO 是入参闸门）。
    """
    return await service.create_session(payload)


@router.get("", response_model=SessionPage, summary="会话列表（最近活跃排序 + 标题搜索）")
async def list_sessions(
    service: ServiceDep,
    title: Annotated[str | None, Query(description="标题子串搜索（AC-004-3）")] = None,
) -> SessionPage:
    """会话列表：按最近活跃倒序，`?title=` 做字面子串搜索（`total` 为过滤后总数）。"""
    return await service.list_sessions(title=title)


@router.get("/{session_id}", response_model=SessionDetail, summary="会话详情（含 run_state）")
async def get_session(session_id: str, service: ServiceDep) -> SessionDetail:
    """会话详情：`run_state` 由 runs 表推导（AC-006-5 挂起态可跨刷新续操作）。

    会话不存在（含路径参数不是合法 UUID）→ 404 SESSION_NOT_FOUND。
    """
    return await service.get_session(session_id)


@router.patch("/{session_id}", response_model=SessionDetail, summary="更新会话（模型/档位热切换）")
async def patch_session(
    session_id: str, payload: SessionPatch, service: ServiceDep
) -> SessionDetail:
    """更新会话：模型/档位写回会话行，run 启动时重读 → 下一条消息起生效（AC-014-2）。

    切到不支持 reasoning 的模型且未显式给档位时档位回落 `"off"`（AC-014-3）；
    空载荷（至少一个字段的契约校验）→ 422 INVALID_ARGUMENT。
    """
    return await service.patch_session(session_id, payload)


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="删除会话（同步 kill 沙箱，AC-002-3）",
)
async def delete_session(session_id: str, runs: RunServiceDep) -> Response:
    """删除会话：停活动 run → kill 沙箱 → 清业务行（子表 → runs → sessions）。

    204 无响应体（契约 response=None；信封中间件按状态码跳过）。会话不存在 → 404
    `SESSION_NOT_FOUND`；沙箱 kill 失败 → 500（会话不删，可重试——不出现「行没了沙箱
    还在计费」的状态）。
    """
    await runs.delete_session(session_id)
    return Response(status_code=204)
