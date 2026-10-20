"""unit：SSE 帧编码与重放→实时衔接（A3 §6.4；test-plan §2.3 AC-003-3、§5 T5）。

判据 = `contracts/src/lcode_contracts/rest.py` 模块 docstring「SSE 传输语义」：
- 帧格式 `id: <seq>` / `event: <type>` / `data: <单行 JSON>`；
- 连接建立即发 `retry: 3000`（SSE_RETRY_MS）；
- 每 15s 注释心跳 `: keep-alive`（本文件用可注入短间隔，不真等 15s）；
- 断线续传：`Last-Event-ID` 优先，其次 `?after_seq=`；无两者从水位 0 全量重放；
- 重放→实时无缝：**先订阅后重放**，队列里 seq ≤ 已发水位的事件按 seq 去重丢弃；
- 慢消费者：订阅队列满 → 关闭该订阅（连接结束，客户端带水位重连补齐，A3 §6.1）。
"""

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from app.core.config import sse_settings
from app.events.bus import SessionEventBus, SubscriptionClosed
from app.events.sse import (
    SSEFrameStream,
    encode_comment,
    encode_frame,
    encode_heartbeat,
    encode_retry,
    resolve_watermark,
)
from lcode_contracts.events import Event

_UTC = UTC
SESSION_ID = "3f1c9a52-6f77-4a0f-9a1e-7b2c4d5e6f70"
_EVENT_SOURCE: Any = Event  # 运行期计算的判别联合（mypy 视作变量非类型），构造实参经 Any 中转
_ADAPTER: TypeAdapter[Any] = TypeAdapter(_EVENT_SOURCE)


def _delta(seq: int, *, delta: str = "hi", session_id: str = SESSION_ID) -> dict[str, Any]:
    return {
        "type": "message_delta",
        "session_id": session_id,
        "run_id": None,
        "seq": seq,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC) + timedelta(seconds=seq),
        "message_id": "msg-1",
        "delta": delta,
    }


def _event(seq: int, **kwargs: Any) -> Any:
    return _ADAPTER.validate_python(_delta(seq, **kwargs))


async def _drain(frames: Any, count: int) -> list[str]:
    return [await anext(frames) for _ in range(count)]


# ---------------------------------------------------------------- 帧格式


def test_frame_format_is_id_event_single_line_data() -> None:
    frame = encode_frame(_event(7, delta="第一行\n\n第二行"))

    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").split("\n")
    assert lines[0] == "id: 7"
    assert lines[1] == "event: message_delta"
    assert len(lines) == 3, "data 必须是单行（多行 delta 由 JSON 转义吸收）"
    assert lines[2].startswith("data: ")
    payload = json.loads(lines[2][len("data: ") :])
    assert payload["seq"] == 7
    assert payload["delta"] == "第一行\n\n第二行"
    _ADAPTER.validate_python(payload)  # 帧内 JSON 必须还是契约事件（回放无损）


def test_retry_and_heartbeat_frames() -> None:
    # 显式值原样透传；无参取 SSE_RETRY_MS 配置（契约默认 3000；.env.test 里 e2e 调成 1000）
    assert encode_retry(3000) == "retry: 3000\n\n"
    assert encode_retry() == f"retry: {sse_settings.retry_ms}\n\n"
    assert encode_retry(1500) == "retry: 1500\n\n"
    assert encode_heartbeat() == ": keep-alive\n\n"
    assert encode_comment("note") == ": note\n\n"


@pytest.mark.parametrize(
    ("last_event_id", "after_seq", "expected"),
    [
        ("5", 9, 5),  # Last-Event-ID 优先于 after_seq（契约语义）
        (None, 9, 9),
        ("5", None, 5),
        (None, None, 0),  # 无两者：从水位 0 全量重放
        ("not-a-number", 9, 9),  # 非法头忽略，退回查询参数
        ("-3", None, 0),  # 负数按 0 起步
        (None, "-1", 0),
        ("0", None, 0),
    ],
)
def test_watermark_resolution_priority(
    last_event_id: str | None, after_seq: Any, expected: int
) -> None:
    assert resolve_watermark(last_event_id, after_seq) == expected


# ---------------------------------------------------------------- 重放 → 实时衔接


async def test_stream_yields_retry_then_replay_then_live_with_seq_dedup() -> None:
    bus = SessionEventBus(queue_size=16)

    async def replay(session_id: str, after_seq: int) -> Any:
        assert session_id == SESSION_ID and after_seq == 0
        for seq in (1, 2):
            yield _event(seq, delta=f"replay-{seq}")

    stream = SSEFrameStream(
        session_id=SESSION_ID, bus=bus, after_seq=0, replay_source=replay, heartbeat_seconds=30
    )
    frames = cast(Any, stream.frames())

    assert await anext(frames) == f"retry: {sse_settings.retry_ms}\n\n"  # 首帧 retry（配置值）
    first, second = await _drain(frames, 2)
    assert json.loads(first.splitlines()[2][6:])["seq"] == 1
    assert json.loads(second.splitlines()[2][6:])["seq"] == 2

    # 重放期间来过的旧事件（seq ≤ 水位）在实时阶段必须丢弃（按 seq 去重）
    bus.publish(_event(2, delta="duplicate"))
    bus.publish(_event(3, delta="live"))
    live = await anext(frames)
    assert json.loads(live.splitlines()[2][6:])["seq"] == 3

    await frames.aclose()


async def test_events_published_during_replay_are_not_lost() -> None:
    """先订阅后重放：重放窗内新落库的事件已入队，重放结束后立刻补发（无缝衔接）。"""
    bus = SessionEventBus(queue_size=16)
    published: list[int] = []

    async def replay(session_id: str, after_seq: int) -> Any:
        yield _event(1, delta="history")
        bus.publish(_event(9, delta="raced"))  # 重放途中新事件
        published.append(9)

    stream = SSEFrameStream(
        session_id=SESSION_ID, bus=bus, after_seq=0, replay_source=replay, heartbeat_seconds=30
    )
    frames = cast(Any, stream.frames())

    await anext(frames)  # retry
    assert json.loads((await anext(frames)).splitlines()[2][6:])["seq"] == 1
    live = await anext(frames)
    assert json.loads(live.splitlines()[2][6:])["seq"] == 9

    await frames.aclose()


async def test_stream_emits_heartbeat_when_idle() -> None:
    bus = SessionEventBus(queue_size=4)

    async def replay(session_id: str, after_seq: int) -> Any:
        return
        yield  # pragma: no cover —— 空异步生成器

    stream = SSEFrameStream(
        session_id=SESSION_ID, bus=bus, after_seq=0, replay_source=replay, heartbeat_seconds=0.02
    )
    frames = cast(Any, stream.frames())

    await anext(frames)  # retry
    assert await anext(frames) == ": keep-alive\n\n"
    assert await anext(frames) == ": keep-alive\n\n"

    await frames.aclose()


async def test_slow_subscriber_stream_ends_on_queue_overflow() -> None:
    """慢消费者：队列满即关闭该订阅 → 流结束（客户端带 Last-Event-ID 重连补齐）。"""
    bus = SessionEventBus(queue_size=1)

    async def replay(session_id: str, after_seq: int) -> Any:
        return
        yield  # pragma: no cover

    stream = SSEFrameStream(
        session_id=SESSION_ID, bus=bus, after_seq=0, replay_source=replay, heartbeat_seconds=30
    )
    frames = cast(Any, stream.frames())

    await anext(frames)  # retry
    bus.publish(_event(1))  # 入队
    bus.publish(_event(2))  # 溢出 → 订阅被关闭
    assert bus.subscriber_count(SESSION_ID) == 0

    with pytest.raises(StopAsyncIteration):
        await anext(frames)


async def test_stream_unsubscribes_on_close() -> None:
    bus = SessionEventBus(queue_size=4)

    async def replay(session_id: str, after_seq: int) -> Any:
        return
        yield  # pragma: no cover

    stream = SSEFrameStream(
        session_id=SESSION_ID, bus=bus, after_seq=0, replay_source=replay, heartbeat_seconds=30
    )
    frames = cast(Any, stream.frames())
    await anext(frames)
    assert bus.subscriber_count(SESSION_ID) == 1

    await frames.aclose()
    assert bus.subscriber_count(SESSION_ID) == 0


async def test_watermark_is_passed_to_replay_source() -> None:
    """断线重连：Last-Event-ID 解析出的水位必须原样传给重放（不重发已收到内容）。"""
    bus = SessionEventBus(queue_size=4)
    seen: list[int] = []

    async def replay(session_id: str, after_seq: int) -> Any:
        seen.append(after_seq)
        yield _event(50)  # 水位 42 之后的下一批

    stream = SSEFrameStream(
        session_id=SESSION_ID,
        bus=bus,
        after_seq=resolve_watermark("42", None),
        replay_source=replay,
        heartbeat_seconds=30,
    )
    frames = cast(Any, stream.frames())
    await anext(frames)  # retry
    replayed = await anext(frames)

    assert seen == [42]
    assert json.loads(replayed.splitlines()[2][6:])["seq"] == 50
    await frames.aclose()


# ---------------------------------------------------------------- 订阅关闭语义（bus）


async def test_subscription_raises_closed_after_overflow() -> None:
    bus = SessionEventBus(queue_size=1)
    subscription = bus.subscribe(SESSION_ID)
    bus.publish(_event(1))
    bus.publish(_event(2))  # 溢出

    assert subscription.closed is True
    with pytest.raises(SubscriptionClosed):
        await subscription.get()


async def test_multiple_subscribers_receive_same_event() -> None:
    """多标签页：同一事件扇出给全部订阅者（进程内总线语义）。"""
    bus = SessionEventBus(queue_size=4)
    first = bus.subscribe(SESSION_ID)
    second = bus.subscribe(SESSION_ID)

    delivered = bus.publish(_event(1))

    assert delivered == 2
    assert (await first.get()).seq == 1
    assert (await second.get()).seq == 1
    assert inspect.iscoroutinefunction(bus.publish) is False  # 进程内广播：无 I/O，无需 await
