"""SSE 帧编码与「重放→实时」衔接（A3 §6.4；契约 `rest.py` 模块 docstring 的传输语义落点）。

帧格式（契约钉死，逐条对齐）：
    retry: 3000            ← 连接建立即发的首帧（SSE_RETRY_MS）
    id: <seq>
    event: <type>
    data: <单行 JSON>       ← `model_dump_json()` 产物（字符串内换行已被 JSON 转义吸收）

心跳：每 `SSE_HEARTBEAT_SECONDS`（默认 15s）发注释行 `: keep-alive`——不产生事件、
不触碰 seq，仅防代理/空闲超时断链。

衔接顺序（A3 §6.4 第 4 条，顺序不能反）：
    ① 先订阅总线（重放窗口内新事件不丢，全部进订阅队列）→
    ② 重放：events 表按 `seq > 水位` 升序批量下发（replay.py）→
    ③ 实时：消费订阅队列，`seq <= 已发水位` 的事件**按 seq 去重丢弃**（重放窗口与实时
       窗口的重叠段只发一次）→
    ④ 订阅被关闭（慢消费者队列溢出）→ 流结束，客户端 EventSource 自动带 Last-Event-ID
       重连，落差由重放补齐。

水位解析：`Last-Event-ID` 请求头**优先**，其次 `?after_seq=`（契约语义）；无两者从水位
0 全量重放（刷新重建整屏，AC-008-3）。非法值（非整数/负数）按缺省处理，不抛错——
SSE 是尽力而为的续传通道，水位解析失败只意味着多补一点，不该拒连。

本模块**不实现 HTTP 端点**（流式接口车道负责 `StreamingResponse` 组装）；这里只提供
帧编码函数与可注入的 `SSEFrameStream`（重放源/总线/心跳/批量大小都可替换，测试零 DB）。
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from app.core.config import sse_settings
from app.events.bus import SessionEventBus, Subscription, SubscriptionClosed
from app.events.emitter import event_type_of
from app.events.replay import DEFAULT_BATCH_SIZE, iter_events_after

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from lcode_contracts.events import BaseEvent

HEARTBEAT_TEXT = "keep-alive"
"""注释心跳的固定文本（契约 rest.py：`: keep-alive`）。"""


def resolve_watermark(last_event_id: str | None, after_seq: object) -> int:
    """断线续传水位：`Last-Event-ID` 头优先，其次 `after_seq`；无/非法一律 0。"""
    for candidate in (last_event_id, after_seq):
        if candidate is None:
            continue
        try:
            value = int(str(candidate).strip())
        except TypeError, ValueError:
            continue  # 非法值按缺省处理（尽力续传，不拒连）
        return max(value, 0)
    return 0


def encode_retry(retry_ms: int | None = None) -> str:
    """首帧 retry（断线后客户端等多久重连；契约默认 3000ms）。"""
    value = sse_settings.retry_ms if retry_ms is None else retry_ms
    return f"retry: {value}\n\n"


def encode_comment(text: str) -> str:
    """SSE 注释行（以 `:` 开头，客户端不当作事件）。"""
    return f": {text}\n\n"


def encode_heartbeat() -> str:
    """空闲心跳帧（固定 `: keep-alive`，不携带任何事件语义）。"""
    return encode_comment(HEARTBEAT_TEXT)


def encode_frame(event: BaseEvent) -> str:
    """契约事件 → SSE 帧（`id`/`event`/`data` 三行 + 空行结尾；data 恒为单行 JSON）。"""
    return f"id: {event.seq}\nevent: {event_type_of(event)}\ndata: {event.model_dump_json()}\n\n"


ReplaySource = Callable[[str, int], AsyncIterator["BaseEvent"]]
"""重放源：`(session_id, after_seq) -> 异步事件流`（生产 = `replay.iter_events_after`）。"""


class SSEFrameStream:
    """一次 SSE 连接的帧流：首帧 retry → 重放 → 实时（含心跳）。

    只产出字符串帧，不关心 HTTP——流式接口车道把它交给 `StreamingResponse` 即可；
    测试可注入 `replay_source` / `bus` / `heartbeat_seconds`，零 DB 零等待。
    """

    def __init__(
        self,
        *,
        session_id: str,
        bus: SessionEventBus | None = None,
        after_seq: int = 0,
        replay_source: ReplaySource | None = None,
        heartbeat_seconds: float | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_id = session_id
        self._bus = bus if bus is not None else SessionEventBus()
        # 水位下界 -1（=「从头全量重放」，replay.iter_events_after 同口径）：钳到 0 会把
        # 「先订阅后发消息」场景的 seq=0 事件（首个 message_created）在实时段按去重丢弃
        # ——实测踩坑（e2e test_send_message_streams_reply 首事件变 sandbox_output）。
        self._after_seq = max(int(after_seq), -1)
        self._replay_source: ReplaySource = replay_source or self._default_replay_source(
            batch_size, session_factory
        )
        self._heartbeat_seconds = (
            float(heartbeat_seconds)
            if heartbeat_seconds is not None
            else float(sse_settings.heartbeat_seconds)
        )
        self._batch_size = batch_size

    # ------------------------------------------------------------ 对外

    async def frames(self) -> AsyncIterator[str]:
        """完整帧流：retry → 重放 → 实时；结束/异常时都退订（连接生命周期内有效）。"""
        subscription = self._bus.subscribe(self._session_id)  # ① 先订阅（先于重放，不丢新事件）
        try:
            yield encode_retry()
            high = self._after_seq
            async for event in self._replay_source(self._session_id, self._after_seq):  # ② 重放
                yield encode_frame(event)
                high = max(high, event.seq)
            async for frame in self._live_frames(subscription, high):  # ③④ 实时 + 心跳/断连
                yield frame
        finally:
            self._bus.unsubscribe(subscription)  # 退出即摘除（关停/断开/生成器关闭都覆盖）

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _default_replay_source(
        batch_size: int, session_factory: async_sessionmaker[AsyncSession] | None
    ) -> ReplaySource:
        def source(session_id: str, after_seq: int) -> AsyncIterator[BaseEvent]:
            return iter_events_after(
                session_id, after_seq, batch_size=batch_size, session_factory=session_factory
            )

        return source

    async def _live_frames(self, subscription: Subscription, watermark: int) -> AsyncIterator[str]:
        """实时段：带心跳地消费订阅队列；seq ≤ 水位的丢弃（衔接去重）；关连即收流。"""
        high = watermark
        while True:
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=self._heartbeat_seconds)
            except TimeoutError:
                yield encode_heartbeat()  # 空闲心跳：不产生事件
                continue
            except SubscriptionClosed:
                return  # 队列溢出/关停：流结束，客户端带水位重连补齐
            if event.seq <= high:  # 重放窗口重叠段：按 seq 去重
                continue
            high = event.seq
            yield encode_frame(event)
