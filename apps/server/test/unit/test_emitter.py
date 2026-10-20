"""unit：事件出口 EventPipeline（A3 §6.1/§6.3/§6.5；test-plan §2.3 AC-003-4、§5 T5）。

判据（A3 §6.1 出口顺序，逐条钉死）：
    构造载荷 → TypeAdapter(Event) 校验（不过不发）→ seq 分配 + INSERT（单条 SQL 内
    `COALESCE(MAX(seq), -1) + 1`）→ COMMIT → bus.publish（**先落库后广播**）

并发（§6.3）：MAX+1 的竞态由 `(session_id, seq)` 唯一索引兜底 —— 唯一冲突时有界重试
（默认 4 次尝试）+ 随机抖动；超限报 SEQ_ALLOCATION（映射 INTERNAL）。本文件用脚本化
假会话把「冲突 → 重试 → 成功」与「冲突耗尽 → 抛错且不广播」两条路径都跑到；真库并发
验证在 `test/integration/test_seq_allocation.py`（requires_db）。

P0 守卫（§6.5 #1/#2）：browser_action 等 P1 占位事件在出口被拒（物理上发不出去）。
"""

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.events.bus import SessionEventBus
from app.events.emitter import (
    EventPipeline,
    NonP0EventError,
    SeqAllocationError,
)
from lcode_contracts.common import AgentRef
from lcode_contracts.errors import ToolErrorObj
from lcode_contracts.events import Event

_UTC = UTC

_EVENT_SOURCE: Any = Event  # 运行期计算的判别联合（mypy 视作变量非类型），构造实参经 Any 中转
_EMITTER_ADAPTER: TypeAdapter[Any] = TypeAdapter(_EVENT_SOURCE)
SESSION_A = "3f1c9a52-6f77-4a0f-9a1e-7b2c4d5e6f70"
SESSION_B = "b2c3d4e5-0000-4111-8222-333344445555"
_ENVELOPE_KEYS = {"seq", "session_id", "run_id", "agent", "type", "created_at"}


# ---------------------------------------------------------------- 假会话工厂（脚本化 DB）


class _FakeSession:
    """最小 AsyncSession 替身：scalar 记录语句与参数，commit/rollback 打标。"""

    def __init__(self, factory: _FakeFactory) -> None:
        self._factory = factory
        self.statements: list[Any] = []
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return await self._factory.insert(statement)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeFactory:
    """按会话递增返回 seq（模拟 MAX+1），并可按剧本先失败 N 次（模拟唯一索引冲突）。"""

    def __init__(self, *, conflicts: int = 0) -> None:
        self._counters: dict[str, Iterator[int]] = defaultdict(lambda: iter(range(10_000)))
        self._conflicts_left = conflicts
        self.sessions: list[_FakeSession] = []
        self.rows: list[dict[str, Any]] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self)
        self.sessions.append(session)
        return session

    async def insert(self, statement: Any) -> int:
        if self._conflicts_left > 0:
            self._conflicts_left -= 1
            raise IntegrityError(
                "INSERT INTO events",
                {},
                Exception('duplicate key value violates unique constraint "uq_events_session_seq"'),
            )
        params = statement.compile(dialect=postgresql.dialect()).params
        session_id = str(params["session_id"])
        seq = next(self._counters[session_id])
        self.rows.append({**params, "seq": seq})
        return seq


class _RecordingBus(SessionEventBus):
    """记录广播顺序，并断言「先落库后广播」：publish 时本次会话必须已 commit。"""

    def __init__(self, factory: _FakeFactory) -> None:
        super().__init__(queue_size=10)
        self._factory = factory
        self.published: list[Any] = []

    def publish(self, event: Any) -> int:
        assert self._factory.sessions and self._factory.sessions[-1].committed, "广播必须先落库"
        self.published.append(event)
        return super().publish(event)


def _pipeline(
    factory: _FakeFactory,
    *,
    bus: SessionEventBus | None = None,
    conflicts: int | None = None,
    max_attempts: int = 4,
    sleeps: list[float] | None = None,
    jitter_seconds: float = 0.02,
    **kwargs: Any,
) -> EventPipeline:
    factory = factory if conflicts is None else _FakeFactory(conflicts=conflicts)
    recorded = sleeps if sleeps is not None else []

    async def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    return EventPipeline(
        bus=bus,
        session_factory=factory,  # type: ignore[arg-type]
        max_attempts=max_attempts,
        jitter=lambda: jitter_seconds,  # 抖动源可注入：测试按剧本值断言
        sleep=_sleep,
        **kwargs,
    )


def _agent_message_delta(delta: str = "hi") -> dict[str, Any]:
    return {
        "type": "message_delta",
        "session_id": SESSION_A,
        "run_id": None,
        "seq": 0,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC),
        "message_id": "msg-1",
        "delta": delta,
    }


# ---------------------------------------------------------------- 正常路径


async def test_emit_assigns_monotonic_seq_per_session() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    first = await pipeline.emit(_agent_message_delta("a"))
    second = await pipeline.emit(_agent_message_delta("b"))
    other_session = await pipeline.emit({**_agent_message_delta("c"), "session_id": SESSION_B})

    assert (first.seq, second.seq) == (0, 1)  # 会话内单调递增
    assert other_session.seq == 0  # seq 是会话内序号，不同会话各自从 0 起
    assert [row["seq"] for row in factory.rows] == [0, 1, 0]
    _EMITTER_ADAPTER.validate_python(first.model_dump(mode="python"))


async def test_emit_split_envelope_columns_and_data_payload() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory)
    event = {
        "type": "tool_call_finished",
        "session_id": SESSION_A,
        "run_id": "9d8e7f60-1a2b-4c3d-8e4f-5a6b7c8d9e01",
        "seq": 0,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC),
        "agent": AgentRef(agent_id="worker:def", role="explorer"),
        "tool_call_id": "call-1",
        "status": "error",
        "duration_ms": 5,
        "error": ToolErrorObj(code="timeout", message="工具执行超时", retryable=True),
    }

    stored = cast(Any, await pipeline.emit(event))

    assert stored.type == "tool_call_finished"
    row = factory.rows[0]
    assert row["session_id"] == uuid.UUID(SESSION_A)
    assert str(row["run_id"]) == event["run_id"]
    assert row["agent"] == {
        "agent_id": "worker:def",
        "role": "explorer",
        "parent_agent_id": None,
        "spawned_by_tool_call_id": None,
    }
    assert row["type"] == "tool_call_finished"
    assert row["created_at"] == event["created_at"]  # 信封时间戳入库，重放与实时逐字节一致
    assert _ENVELOPE_KEYS.isdisjoint(row["data"])  # data = 除信封列外的载荷
    assert row["data"]["tool_call_id"] == "call-1"
    assert row["data"]["error"]["code"] == "timeout"
    _EMITTER_ADAPTER.validate_python(stored.model_dump(mode="python"))


async def test_insert_statement_uses_max_plus_one_and_returning() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    await pipeline.emit(_agent_message_delta())

    compiled = str(
        factory.sessions[0]
        .statements[0]
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )
    normalized = " ".join(compiled.split()).lower()
    assert normalized.startswith("insert into events")
    assert "coalesce(max(events.seq)" in normalized  # seq = MAX+1（单条 SQL 内分配）
    assert "returning events.seq" in normalized  # 分配结果随 INSERT 返回


async def test_emit_publishes_after_commit_and_reaches_subscribers() -> None:
    factory = _FakeFactory()
    bus = _RecordingBus(factory)
    pipeline = _pipeline(factory, bus=bus)
    subscription = bus.subscribe(SESSION_A)

    stored = await pipeline.emit(_agent_message_delta("已提交"))

    assert bus.published == [stored]
    assert subscription.queue.empty() is False
    received = await subscription.get()
    assert received.seq == stored.seq
    await subscription.aclose()


async def test_emit_without_subscribers_still_persists() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory, bus=SessionEventBus(queue_size=4))

    stored = await pipeline.emit(_agent_message_delta())

    assert stored.seq == 0
    assert factory.rows  # 落库独立于有没有人听


# ---------------------------------------------------------------- 冲突重试


async def test_emit_retries_on_unique_conflict_with_jitter() -> None:
    factory = _FakeFactory(conflicts=2)
    sleeps: list[float] = []
    pipeline = _pipeline(factory, max_attempts=4, sleeps=sleeps, jitter_seconds=0.02)

    stored = await pipeline.emit(_agent_message_delta())

    assert stored.seq == 0
    assert len(factory.sessions) == 3  # 冲突 2 次后第 3 次成功
    assert factory.sessions[0].rolled_back and factory.sessions[1].rolled_back
    assert sleeps == [0.02, 0.02]  # 每次重试前抖动退避（默认区间 5~40ms，可注入）


async def test_emit_raises_seq_allocation_error_after_exhausting_retries() -> None:
    factory = _FakeFactory(conflicts=99)
    bus = _RecordingBus(factory)
    pipeline = _pipeline(factory, bus=bus, max_attempts=3)

    with pytest.raises(SeqAllocationError):
        await pipeline.emit(_agent_message_delta())

    assert len(factory.sessions) == 3  # 有界重试：尝试次数上限
    assert bus.published == []  # 没落库就绝不广播


async def test_emit_does_not_retry_non_unique_errors() -> None:
    """非唯一冲突的库错（如外键/类型错）是确定性失败，重试无意义：直接冒泡。"""

    class _RaisingFactory(_FakeFactory):
        async def insert(self, statement: Any) -> int:
            raise IntegrityError(
                "INSERT INTO events", {}, Exception("null value violates not-null")
            )

    factory = _RaisingFactory()
    pipeline = _pipeline(factory, max_attempts=4)

    with pytest.raises(IntegrityError):
        await pipeline.emit(_agent_message_delta())
    assert len(factory.sessions) == 1


# ---------------------------------------------------------------- 出口守卫


async def test_emit_rejects_raw_framework_payload() -> None:
    """AC-008-2 负例：原始 AIMessageChunk 直发被拒（不透传框架原始事件）。"""
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    with pytest.raises(TypeError):
        await pipeline.emit(
            cast(Any, AIMessageChunk(content="hello"))
        )  # 负例：原始框架载荷按 wire 面投喂
    assert factory.rows == []


async def test_emit_rejects_unknown_event_type_and_missing_fields() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    with pytest.raises(ValidationError):
        await pipeline.emit({**_agent_message_delta(), "type": "not_a_contract_event"})

    payload = _agent_message_delta()
    del payload["delta"]
    with pytest.raises(ValidationError):
        await pipeline.emit(payload)
    assert factory.rows == []


async def test_emit_rejects_extra_fields_on_contract_events() -> None:
    """契约 extra="forbid"：拼错字段名的“私货”必须被拒（ADR-003 唯一闸门）。"""
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    with pytest.raises(ValidationError):
        await pipeline.emit({**_agent_message_delta(), "reasoning_content": "偷偷加的字段"})
    assert factory.rows == []


async def test_emit_rejects_browser_action_by_p0_guard() -> None:
    factory = _FakeFactory()
    pipeline = _pipeline(factory)
    payload = {
        "type": "browser_action",
        "session_id": SESSION_A,
        "seq": 0,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC),
        "action": "navigate",
        "status": "started",
    }

    with pytest.raises(NonP0EventError):
        await pipeline.emit(payload)
    assert factory.rows == []


async def test_emit_requires_uuid_session_id() -> None:
    """events.session_id 是 Uuid 列并外键到 sessions.id：非 UUID 字符串在出口即拒。"""
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    with pytest.raises(ValueError):
        await pipeline.emit({**_agent_message_delta(), "session_id": "not-a-uuid"})
    assert factory.rows == []


async def test_emit_is_asyncio_safe_under_concurrency() -> None:
    """同一 pipeline 并发发射：假工厂的 seq 分配互不串台（真库并发见 integration）。"""
    factory = _FakeFactory()
    pipeline = _pipeline(factory)

    stored = await asyncio.gather(*(pipeline.emit(_agent_message_delta(f"d{i}")) for i in range(5)))

    assert sorted(event.seq for event in stored) == [0, 1, 2, 3, 4]
