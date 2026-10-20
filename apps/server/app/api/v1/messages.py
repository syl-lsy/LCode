"""消息端点（AC-001-1/4；A3 §10 任务 7 流式接口车道）。

| 方法 | 路径 | 请求 → 响应 |
|---|---|---|
| POST | `/api/v1/sessions/{session_id}/messages` | 消息 → `MessageSendAck`（event_seq = 水位） |
| GET  | `/api/v1/sessions/{session_id}/messages` | → `MessagePage`（键集游标分页，导出/检索用） |

请求体说明：本文件声明**传输壳** `_MessageBody`（仅 `content: str`，extra=forbid）——
空白判定的首位检查在 message_service（AC-001-4 的契约区分度：空白 → `MESSAGE_EMPTY`/422），
随后由 service 过契约 `MessageCreate` 结构闸门（私货字段/类型错误仍 → `INVALID_ARGUMENT`）。
若直接以 `MessageCreate` 作解析闸门，其 validator 会在 service 之前拦截空白，端点声明的
`MESSAGE_EMPTY` 将不可达（A3 §8.1「validator 是二道防线」裁定的落地形态）。

约定（基座铁律 3/8/9）：路由只做路由 + 参数校验 + 依赖注入，业务在 service；成功响应由
`/api/` 信封中间件统一包装；错误一律走全局异常处理器出口，不手拼 JSONResponse、不碰 ORM。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import EventPipelineDep, RunServiceDep, get_db
from app.services.message_service import MessageService
from lcode_contracts.common import ContractModel
from lcode_contracts.rest import MessagePage, MessageSendAck

router = APIRouter(tags=["messages"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
"""请求级事务会话（`message_service.post_message` 在 emit 前显式 commit，A3 §7.3）。"""


class _MessageBody(ContractModel):
    """POST /messages 的传输壳：结构闸门（extra=forbid）+ 空白留给 service 首位检查。"""

    content: str


def get_message_service(
    db: DbDep, pipeline: EventPipelineDep, run_service: RunServiceDep
) -> MessageService:
    """装配消息 service：发射管线 + run 启动器（`run_service.start_run`，同步调度不阻塞）。"""
    return MessageService(db, pipeline=pipeline, run_starter=run_service.start_run)


MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageSendAck,
    summary="发送用户消息（启动 run）",
)
async def send_message(
    session_id: str, payload: _MessageBody, service: MessageServiceDep
) -> MessageSendAck:
    """发送消息：业务行落库 → commit → emit `message_created` → ack（A3 §6.1 钉死顺序）。

    空白 → 422 `MESSAGE_EMPTY`；会话不存在 → 404 `SESSION_NOT_FOUND`；已有活动 run →
    409 `RUN_ALREADY_ACTIVE`（P0 会话级串行）。`event_seq` 是当前会话事件水位——客户端
    立即以 `?after_seq=event_seq` 订阅 SSE，落差由重放补齐（契约 MessageSendAck 注释）。
    """
    return await service.post_message(session_id, payload.content)


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessagePage,
    summary="消息历史（游标分页）",
)
async def list_messages(
    session_id: str,
    service: MessageServiceDep,
    cursor: Annotated[
        str | None, Query(description="游标：上一页响应的 next_cursor（缺省取第一页）")
    ] = None,
) -> MessagePage:
    """消息历史投影（不含 thinking）：最新在前，`next_cursor` 为空表示尾页。"""
    return await service.list_messages(session_id, cursor=cursor)


__all__ = ["MessageServiceDep", "router"]
