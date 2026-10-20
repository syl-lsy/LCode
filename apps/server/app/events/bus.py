"""进程内 per-session 事件总线（A3 §6.1 `SessionEventBus`；REQ-003 / AC-003-3）。

语义（契约 `rest.py` 的 SSE 传输语义 + A3 §6.1）：

- **多订阅者扇出**：`dict[session_id, set[Subscription]]`——同一会话多标签页各有自己的
  有界队列（`SSE_SUBSCRIBER_QUEUE`，默认 1000），互不拖累；
- **慢消费者 = 断连，不做无条件缓冲**：队列满 → 关闭该订阅（消费方结束流，客户端
  EventSource 自动带 `Last-Event-ID` 重连，落差由 events 表重放补齐）。这是「先落库
  后广播」架构买来的简化：总线只负责送达，**真相在库里**；
- **无 I/O**：`publish()` 是同步方法（进程内 `Queue.put_nowait`），因此不 await 也不会
  拖慢发射方；`emit` 已保证「先落库后广播」，广播失败（订阅已满）不影响事件事实；
- 进程重启后总线为空——无所谓，DB 重放即真相（AC-008-3）。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app.core.config import sse_settings

if TYPE_CHECKING:  # 仅类型用途：契约事件是纯数据，运行期不需要额外 import 成本
    from lcode_contracts.events import BaseEvent


class SubscriptionClosed(Exception):
    """订阅已被关闭（队列溢出 / 服务关停）：消费方据此结束流（客户端靠重连重放补齐）。"""

    def __init__(self, session_id: str, reason: str) -> None:
        super().__init__(f"订阅已关闭（session={session_id}, reason={reason}）")
        self.session_id = session_id
        self.reason = reason


class Subscription:
    """一个订阅者的有界队列（异步迭代 = 逐条取事件；关闭后 `get()` 抛 `SubscriptionClosed`）。"""

    def __init__(self, session_id: str, *, queue_size: int) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[BaseEvent] = asyncio.Queue(maxsize=queue_size)
        self.closed = False
        self.closed_reason: str | None = None

    # ------------------------------------------------------------ 内部（bus 调用）

    def mark_closed(self, reason: str) -> None:
        """标记关闭（幂等）：已入队的事件不再送达，消费方重新订阅+重放。"""
        self.closed = True
        self.closed_reason = reason

    def offer(self, event: BaseEvent) -> bool:
        """尝试入队；队列满 → 关闭本订阅并返回 False（调用方据此把它摘出注册表）。"""
        if self.closed:
            return False
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.mark_closed("queue_overflow")
            return False
        return True

    # ------------------------------------------------------------ 对外

    async def get(self) -> BaseEvent:
        """取一条事件；订阅已关闭时抛 `SubscriptionClosed`（不阻塞、不排空残留队列）。"""
        if self.closed:
            raise SubscriptionClosed(self.session_id, self.closed_reason or "closed")
        return await self.queue.get()

    def __aiter__(self) -> AsyncIterator[BaseEvent]:
        return self

    async def __anext__(self) -> BaseEvent:
        """异步迭代：关闭即 StopAsyncIteration（`async for` 自然结束）。"""
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def aclose(self, reason: str = "client_closed") -> None:
        """消费方主动退订（SSE 连接结束 / 生成器收尾）。"""
        self.mark_closed(reason)


class SessionEventBus:
    """按会话隔离的进程内广播总线（无 I/O、无锁：单事件循环内 set 增删是原子的）。"""

    def __init__(self, *, queue_size: int | None = None) -> None:
        self._queue_size = queue_size if queue_size is not None else sse_settings.subscriber_queue
        self._subscribers: dict[str, set[Subscription]] = {}

    # ------------------------------------------------------------ 订阅面

    def subscribe(self, session_id: str) -> Subscription:
        """新建订阅（SSE 端点建连时调用；**先订阅再重放**，见 sse.py 的衔接顺序）。"""
        subscription = Subscription(session_id, queue_size=self._queue_size)
        self._subscribers.setdefault(session_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """摘除订阅（幂等）；空集合顺手清掉，避免注册表长跑后留空键。"""
        subscription.mark_closed("unsubscribed")
        subscribers = self._subscribers.get(subscription.session_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._subscribers.pop(subscription.session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        """当前订阅者数（可观测性/测试锚点；连接状态提示也用它）。"""
        return len(self._subscribers.get(session_id, ()))

    def close_all(self, reason: str = "shutdown") -> None:
        """服务关停：关闭全部订阅（SSE 流随之结束，客户端重连后由 DB 重放补齐）。"""
        for subscribers in self._subscribers.values():
            for subscription in subscribers:
                subscription.mark_closed(reason)
        self._subscribers.clear()

    # ------------------------------------------------------------ 广播面

    def publish(self, event: BaseEvent) -> int:
        """把已落库的事件广播给该会话的订阅者，返回送达数（溢出者被摘除）。

        注意：**只在 emit 的 COMMIT 之后调用**（A3 §6.1「先落库后广播」）——广播出去
        的事件必须已经是可重放的事实，否则断线重连会在库里找不到它。
        """
        session_id = str(getattr(event, "session_id", ""))
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return 0
        delivered = 0
        overflowed: list[Subscription] = []
        for subscription in tuple(subscribers):  # 快照遍历：offer 不改集合本身
            if subscription.offer(event):
                delivered += 1
            else:
                overflowed.append(subscription)
        for subscription in overflowed:
            subscribers.discard(subscription)
        if not subscribers:
            self._subscribers.pop(session_id, None)
        return delivered
