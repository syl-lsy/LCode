"""integration：seq 分配与重放（A3 §6.3；test-plan §2.3 AC-003-4 的库侧锚、§5 T5）。

判据：
- seq = 会话内单调递增（只保序不保密度），`(session_id, seq)` 唯一索引兜底；
- MAX+1 竞态真实存在（run 内扇出 Send×N / 并行 tool_calls）→ 有界重试 + 抖动；
- 每事件独立短事务：COMMIT 成功后才广播（先落库后广播）；
- 重放（replay.py）按 seq 升序批量读 `seq > 水位`，解析结果必须过契约 `TypeAdapter(Event)`
  ——「重放与实时同源无缝」的库侧证据。

库名：本车道用 `DB_NAME=lcode_events_test`（与同批并发车道隔离；**不改** conftest）。
库不可达 / DB_NAME 非 `_test` 时由 conftest 显式 skip（-ra 可见 reason）。
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import TypeAdapter
from sqlalchemy import func, select

from app.events.bus import SessionEventBus
from app.events.emitter import EventPipeline
from app.events.replay import fetch_events_after
from app.models import Event as EventRow
from app.models import Session
from lcode_contracts.events import Event

pytestmark = pytest.mark.requires_db

_UTC = UTC
_EVENT_SOURCE: Any = Event  # 运行期计算的判别联合（mypy 视作变量非类型），构造实参经 Any 中转
_ADAPTER: TypeAdapter[Any] = TypeAdapter(_EVENT_SOURCE)
CONCURRENT_EMITS = 12


def _delta(session_id: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "type": "message_delta",
        "session_id": session_id,
        "run_id": None,
        "seq": 0,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC),
        "message_id": "msg-1",
        "delta": "hi",
    }
    payload.update(overrides)
    return payload


async def _seed_session(db_session: Any, model: str = "qwen3.7-plus") -> str:
    row = Session(model=model, reasoning_effort="medium", sandbox_provider="bailian")
    db_session.add(row)
    await db_session.commit()
    return str(row.id)


async def _rows_for_session(db_session: Any, session_id: str) -> list[EventRow]:
    """按 seq 升序取该会话事件行（ORM 表达式查询：参数绑定，无字符串拼接）。"""
    statement = select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.seq)
    return list(await db_session.scalars(statement))


async def _count_for_session(db_session: Any, session_id: str) -> int:
    statement = select(func.count()).select_from(EventRow).where(EventRow.session_id == session_id)
    return int(await db_session.scalar(statement) or 0)


async def test_emit_assigns_consecutive_seq_and_row_round_trips(db_session: Any) -> None:
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))

    stored = [await pipeline.emit(_delta(session_id, delta=f"d{index}")) for index in range(3)]

    assert [event.seq for event in stored] == [0, 1, 2]
    rows = await _rows_for_session(db_session, session_id)
    assert [row.seq for row in rows] == [0, 1, 2]
    assert rows[0].type == "message_delta"
    assert rows[0].data["delta"] == "d0"
    assert "seq" not in rows[0].data and "type" not in rows[0].data  # data = 除信封列外载荷


async def test_concurrent_emits_never_duplicate_seq(db_session: Any) -> None:
    """并发发射（fan_out/并行 tool_calls 的现实）：唯一索引兜底 + 有界重试后零重复。"""
    session_id = await _seed_session(db_session)
    bus = SessionEventBus(queue_size=128)
    pipeline = EventPipeline(bus=bus)
    subscription = bus.subscribe(session_id)

    stored = await asyncio.gather(
        *(pipeline.emit(_delta(session_id, delta=f"c{index}")) for index in range(CONCURRENT_EMITS))
    )

    seqs = sorted(event.seq for event in stored)
    assert seqs == list(range(CONCURRENT_EMITS))  # 无重复、无空洞（本场景无回滚）
    assert await _count_for_session(db_session, session_id) == CONCURRENT_EMITS
    delivered = {event.seq for event in [await subscription.get() for _ in range(CONCURRENT_EMITS)]}
    assert delivered == set(seqs)  # 广播的都是已落库事实（先落库后广播）
    await subscription.aclose()


async def test_forced_collision_retries_then_succeeds(db_session: Any, monkeypatch: Any) -> None:
    """确定性重试路径：首次分配故意落后一位（模拟竞态里被抢走的 seq）→ 冲突 → 重试成功。"""
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    await pipeline.emit(_delta(session_id, delta="first"))  # 水位 = 0

    original = EventPipeline._seq_expression
    attempts: list[int] = []

    def flaky(self: EventPipeline, sid: Any, *, offset: int = 1) -> Any:
        effective = 0 if not attempts else offset  # 首次故意落后一位，其后按正常 MAX+1
        attempts.append(effective)
        return original(self, sid, offset=effective)

    monkeypatch.setattr(EventPipeline, "_seq_expression", flaky)
    stored = await pipeline.emit(_delta(session_id, delta="retried"))

    assert attempts == [0, 1]  # 先撞车再回到 MAX+1（重试路径真实走过一次唯一冲突）
    assert stored.seq == 1


async def test_seq_is_allocated_per_session(db_session: Any) -> None:
    first_session = await _seed_session(db_session)
    second_session = await _seed_session(db_session, model="qwen3.8-flash")
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))

    first = await pipeline.emit(_delta(first_session))
    second = await pipeline.emit(_delta(first_session))
    other = await pipeline.emit(_delta(second_session))

    assert (first.seq, second.seq, other.seq) == (0, 1, 0)


async def test_replay_returns_ordered_contract_events(db_session: Any) -> None:
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    await pipeline.emit(_delta(session_id, delta="第一"))
    await pipeline.emit(
        {
            "type": "run_status",
            "session_id": session_id,
            "seq": 0,
            "created_at": datetime(2026, 10, 17, 12, 0, 1, tzinfo=_UTC),
            "subject": "run",
            "phase": "started",
        }
    )
    await pipeline.emit(_delta(session_id, delta="第三"))

    replayed = cast(Any, await fetch_events_after(session_id, after_seq=-1))

    assert [event.seq for event in replayed] == [0, 1, 2]
    assert [event.type for event in replayed] == ["message_delta", "run_status", "message_delta"]
    for event in replayed:  # 回放载荷逐条过契约校验（重放与实时同源）
        _ADAPTER.validate_python(event.model_dump(mode="python"))
    assert replayed[0].delta == "第一"
    assert replayed[2].created_at == datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC)


async def test_replay_respects_watermark(db_session: Any) -> None:
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    for index in range(3):
        await pipeline.emit(_delta(session_id, delta=f"e{index}"))

    tail = cast(Any, await fetch_events_after(session_id, after_seq=0))

    assert [event.seq for event in tail] == [1, 2]
    assert [event.delta for event in tail] == ["e1", "e2"]


async def test_replay_batches_are_paged(db_session: Any) -> None:
    """批量读：batch_size 小于总数时分页取完、顺序不变（长会话重放不一次性吃满内存）。"""
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    for index in range(5):
        await pipeline.emit(_delta(session_id, delta=f"b{index}"))

    replayed = cast(Any, await fetch_events_after(session_id, after_seq=-1, batch_size=2))

    assert [event.seq for event in replayed] == [0, 1, 2, 3, 4]
    assert [event.delta for event in replayed] == [f"b{index}" for index in range(5)]
