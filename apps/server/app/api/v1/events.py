"""SSE 事件流端点（AC-003-3/AC-008-3；A3 §6.4；契约 rest.py 模块 docstring 的传输语义）。

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/api/v1/sessions/{session_id}/events` | `text/event-stream`（重放 + 实时，唯一流式端点） |

逐条对齐 A3 §6.4 的实现要点：

1. **流前校验**：会话不存在 → 普通 REST 错误体（`SESSION_NOT_FOUND`/404），不进流
   （校验在返回 `StreamingResponse` **之前**完成，异常走全局处理器出口）；校验用**短会话**
   即开即关——依赖注入的请求级会话会随整条流存活，长连接会把连接池连接钉死；
2. `request.state.is_stream = True`（跳过 `/api/` 成功信封，core/middleware/response）+
   `StreamingResponse(media_type="text/event-stream")`；首帧 `retry` 由帧流发出；
3. **续传水位**：`Last-Event-ID` 请求头**优先**，其次 `?after_seq=`（`resolve_watermark`
   承载该优先级；无两者从 0 全量重放；头的非法值按缺省处理不拒连——尽力续传；
   `after_seq` 查询参数按契约声明为 int，非整数由 FastAPI 拦为 422 `INVALID_ARGUMENT`）；
4. **先订阅再重放**：衔接顺序在 `SSEFrameStream.frames()` 内（订阅 → 重放 → 实时，按 seq
   去重）；本端点只消费其帧，不接触 astream 原始流（A3 §6.5 强制点 3）；
5. **心跳**：帧流内按 `SSE_HEARTBEAT_SECONDS` 注释行 `: keep-alive`（不产生事件）；
6. 客户端断开：逐帧间探测 `request.is_disconnected()`，断开即收流（finally 退订总线）。

本文件不做任何事件构造/校验——帧全部来自 emitter 产物（已过 `TypeAdapter(Event)` 闸门）。
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dependencies import EventPipelineDep, ModelRegistryDep, get_db_factory
from app.events import SSEFrameStream, resolve_watermark
from app.services.session_service import SessionService

router = APIRouter(tags=["events"])

DbFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_db_factory)]
"""短会话工厂（流前一次性校验用；长连接不占用请求级会话/连接池连接）。"""


@router.get(
    "/sessions/{session_id}/events",
    summary="SSE 事件流（重放+实时；Last-Event-ID/after_seq 断线续传）",
)
async def stream_events(
    session_id: str,
    request: Request,
    pipeline: EventPipelineDep,
    registry: ModelRegistryDep,
    db_factory: DbFactoryDep,
    after_seq: Annotated[
        int | None,
        Query(description="断线续传水位（仅当无 Last-Event-ID 请求头时生效；头优先）"),
    ] = None,
) -> StreamingResponse:
    """事件流：流前校验 → 水位解析 →（订阅 → 重放 → 实时 + 心跳）的帧流。"""
    # ① 流前校验（短会话即开即关）：不存在 → 404 SESSION_NOT_FOUND 的普通 REST 错误体
    async with db_factory() as probe:
        await SessionService(probe, registry).get_session(session_id)

    # ② 水位：Last-Event-ID 头优先，其次 after_seq（两者皆缺 = 全量重放）。
    # replay 的口径是「seq > 水位」，从头全量重放的水位是 -1（replay.py docstring）；
    # resolve_watermark 对「缺省」给的 0 表达的是「客户端已收到 seq 0」——两者不可混用：
    # 头/参都缺时映射 -1（刷新重建整屏，AC-008-3），显式 0 仍是续传语义。
    header = request.headers.get("last-event-id")
    watermark = -1 if header is None and after_seq is None else resolve_watermark(header, after_seq)

    # ③ 流式响应：跳过 /api/ 成功信封（is_stream 标志）；帧流 = retry → 重放 → 实时。
    # 水位原样传入（SSEFrameStream 的下界已是 -1 = 从头全量重放，与 replay 口径一致；
    # 此前端点用显式 replay_source 绕开 ≥0 钳制的接线已随钳制修正回归默认）。
    request.state.is_stream = True
    frames = SSEFrameStream(session_id=session_id, bus=pipeline.bus, after_seq=watermark)

    async def frame_stream() -> AsyncIterator[str]:
        async for frame in frames.frames():
            if await request.is_disconnected():
                break
            yield frame

    return StreamingResponse(
        frame_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
