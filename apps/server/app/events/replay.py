"""events 表重放：按 `seq > 水位` 批量读（A3 §6.4 第 4 条；AC-003-3/AC-008-3）。

用途与语义：
- SSE 建连（含断线重连）时**先把持久化事件补齐**再切实时流——「已收到内容不丢」由本模块
  保证（契约 rest.py 的断线续传语义）；
- 水位来源：`Last-Event-ID` 请求头优先、其次 `?after_seq=`（解析在 `sse.resolve_watermark`）；
- 批量分页：`batch_size`（默认 500）条一取，长会话重放不一次性把全部事件读进内存；
  每批一个短会话（只读、不占长事务）；
- **解析即校验**：DB 行 → 契约事件一律过 `TypeAdapter(Event)`，所以「重放与实时同源无缝」
  是可执行断言而不是口头约定（`test/unit/test_sse_encoding.py` 与 integration 都有断言）；
- 行 → 事件的重建口径：信封列（seq/session_id/run_id/agent/type/created_at）来自表列，
  其余载荷来自 `data` jsonb（`models/events.py` docstring：data = 扁平事件去掉信封列）。
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.models.events import Event as EventRow
from lcode_contracts.events import BaseEvent
from lcode_contracts.events import Event as EventUnion

DEFAULT_BATCH_SIZE = 500
"""单批读取行数：够大（一次往返覆盖常见刷新）又够小（长会话 10^3~10^4 条分级取用）。"""

_ADAPTER: TypeAdapter[Any] = TypeAdapter(EventUnion)  # type: ignore[arg-type]  # 判别联合别名：运行期校验成立，mypy 不识作 TypeForm


def row_to_event(row: EventRow) -> BaseEvent:
    """events 行 → 契约事件（判别联合校验，失败即抛——库损坏/契约漂移当场可见）。"""
    payload: dict[str, object] = dict(row.data)  # 载荷（除信封列外）在先，信封列随后覆盖
    payload.update(
        seq=row.seq,
        session_id=str(row.session_id),
        run_id=str(row.run_id) if row.run_id is not None else None,
        agent=row.agent,
        created_at=row.created_at,
        type=row.type,
    )
    event: BaseEvent = _ADAPTER.validate_python(payload)
    return event


async def iter_events_after(
    session_id: str,
    after_seq: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[BaseEvent]:
    """按 seq 升序逐批产出 `seq > after_seq` 的事件（每批一个只读短会话）。

    水位语义：只发**严格大于** `after_seq` 的行（与 SSE id 语义一致——Last-Event-ID 是
    客户端已收到的最后一条 seq）。`after_seq=-1` 表示「从头全量重放」。
    """
    factory = session_factory or get_session_factory()
    key = uuid.UUID(str(session_id))
    watermark = after_seq
    while True:
        async with factory() as session:
            statement = (
                select(EventRow)
                .where(EventRow.session_id == key, EventRow.seq > watermark)
                .order_by(EventRow.seq)
                .limit(batch_size)
            )
            rows = list(await session.scalars(statement))
        if not rows:
            return
        for row in rows:
            event = row_to_event(row)
            watermark = event.seq
            yield event
        if len(rows) < batch_size:
            return


async def fetch_events_after(
    session_id: str,
    after_seq: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list[BaseEvent]:
    """`iter_events_after` 的攒批版（小水位差场景：一次拿全，顺序不变）。"""
    return [
        event
        async for event in iter_events_after(
            session_id, after_seq, batch_size=batch_size, session_factory=session_factory
        )
    ]
