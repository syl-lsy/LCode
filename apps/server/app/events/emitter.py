"""事件出口 EventPipeline（A3 §6.1/§6.3/§6.5）：**全系统唯一事件出口**。

出口顺序（逐条对应 A3 §6.1，顺序本身是契约的一部分）：
    构造载荷 → `TypeAdapter(Event)` 校验（不过不发，ADR-003 唯一闸门）→ P0 守卫 →
    seq 分配（单条 `INSERT ... VALUES ((SELECT COALESCE(MAX(seq), -1) + 1 FROM events
    WHERE session_id = :sid), ...) RETURNING seq`）→ COMMIT → `bus.publish`（**先落库
    后广播**，重放与实时同源无缝）

并发与重试（§6.3）：`RUN_ALREADY_ACTIVE` 只保证会话级单 run，**不保证 run 内发射串行**
（fan_out 的 Send×N、一回合内多个并行 tool_calls 都会同会话并发 emit）——MAX+1 的竞态
真实存在，靠 `(session_id, seq)` 唯一索引兜底：唯一冲突时**有界重试（默认 4 次尝试）+
随机抖动退避**（抖动用 `secrets.SystemRandom`，不可预测才打散重撞窗口）；超限抛
`SeqAllocationError`（由上层映射 INTERNAL）。非唯一冲突的库错（外键/非空等）是确定性
失败，直接冒泡不重试。

事务边界（§7.3）：**每事件一个独立短事务**——`core/db.py` 的会话工厂自取会话，COMMIT
成功才算事件事实成立、才广播；emit 失败不影响调用方已提交的业务行（幽灵事件窗口的
反面：事件永不引用未提交事实）。

P0 守卫（§6.5 #1/#2）：`type` 必须同时在契约注册表与 `P0_EVENT_TYPES` 内——`browser_action`
（P1 占位）在出口被拒，物理上发不出去。
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from secrets import SystemRandom
from typing import Any, TypeVar

from pydantic import TypeAdapter
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events.bus import SessionEventBus
from app.models.events import Event as EventRow
from lcode_contracts.events import P0_EVENT_TYPES, BaseEvent, Event

_ADAPTER: TypeAdapter[BaseEvent] = TypeAdapter(Event)  # type: ignore[arg-type]  # Event 是 Annotated 联合别名（运行期即判别联合，mypy 不识作类型）
"""判别联合的唯一闸门（ADR-003）：任何来源的载荷都经它校验后才允许落库/广播。"""

_EVENT_UNIQUE_INDEX = "uq_events_session_seq"
_ENVELOPE_FIELDS: tuple[str, ...] = (
    "seq",
    "session_id",
    "run_id",
    "agent",
    "type",
    "created_at",
)
"""events 表信封列（`models/events.py`）：`data` jsonb 存**除这些列以外**的载荷。"""

DEFAULT_MAX_ATTEMPTS = 4
"""seq 分配尝试上限（首次 + 最多 3 次重试；A3 §6.3/U6 的「3~5 次」区间取偏小值）。"""

DEFAULT_JITTER_MS: tuple[int, int] = (5, 40)
"""抖动退避区间（毫秒）：泊松式小抖动即可打散并发窗口，且不拖慢正常发射。"""

_EventT = TypeVar("_EventT", bound=BaseEvent)


class EventEmitError(RuntimeError):
    """事件出口失败（上层映射 INTERNAL；原始异常只进日志，不进 wire）。"""


class NonP0EventError(EventEmitError):
    """P0 阶段拒绝产出的事件类型（`browser_action` 等 P1 占位，AC-008-1/ADR-002）。"""


class SeqAllocationError(EventEmitError):
    """seq 分配在重试上限内持续冲突（A3 §6.3：超限报 INTERNAL）。"""


def _default_jitter(low_ms: int, high_ms: int) -> Callable[[], float]:
    """默认抖动源：系统熵源（不可预测；打破并发方反复重撞同一窗口）。"""
    rng = SystemRandom()
    low, high = low_ms / 1000, high_ms / 1000
    return lambda: rng.uniform(low, high)


def validate_event_payload(payload: BaseEvent | Mapping[str, Any]) -> BaseEvent:
    """载荷 → 契约事件（判别联合校验）。

    契约事件模型（pydantic 已校验过一遍）走 `model_dump → 再校验`：这条往返看似冗余，
    换来的是**单一闸门**——无论来源是 translator、service 还是自定义流透传，出口一律
    按 12 型注册表 + `extra="forbid"` 判一遍，原始框架事件（非模型非 mapping）直接拒。
    """
    if isinstance(payload, BaseEvent):
        return _ADAPTER.validate_python(payload.model_dump(mode="python"))
    if isinstance(payload, Mapping):
        return _ADAPTER.validate_python(dict(payload))
    raise TypeError(
        f"事件载荷必须是契约事件模型或 mapping，收到 {type(payload).__name__}"
        "（原始框架事件不得直发，AC-008-2）"
    )


def ensure_p0_event(event: BaseEvent) -> None:
    """P0 守卫：`type` 不在 `P0_EVENT_TYPES` 内即拒（browser_action 是 P1 占位）。"""
    event_type = event_type_of(event)
    if event_type not in P0_EVENT_TYPES:
        raise NonP0EventError(
            f"P0 阶段不得产出 {event_type!r}（P1 占位事件；见 contracts.events.P0_EVENT_TYPES）"
        )


def event_type_of(event: BaseEvent) -> str:
    """事件 type 字面量（判别字段定义在各具体事件类上，基类视角取类级字段默认值）。"""
    return str(type(event).model_fields["type"].default)


def _is_seq_conflict(exc: IntegrityError) -> bool:
    """唯一冲突判定：优先认 asyncpg 的 UniqueViolationError，退回索引名匹配。"""
    if type(getattr(exc, "orig", None)).__name__ == "UniqueViolationError":
        return True
    return _EVENT_UNIQUE_INDEX in str(exc)


def _as_uuid(value: str, *, field: str) -> uuid.UUID:
    """wire 字符串 → uuid（events.session_id 是 Uuid 列且外键 sessions.id）。"""
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError(
            f"{field} 必须是 UUID 字符串（服务端生成的 id 口径），收到 {value!r}"
        ) from exc


class EventPipeline:
    """事件出口：校验 → seq 分配 + 落库（短事务）→ 广播。线程/协程安全（每事件自取会话）。"""

    def __init__(
        self,
        *,
        bus: SessionEventBus | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        jitter_ms: tuple[int, int] = DEFAULT_JITTER_MS,
        jitter: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._bus = bus if bus is not None else SessionEventBus()
        self._session_factory = session_factory
        self._max_attempts = max(1, max_attempts)
        self._jitter = jitter if jitter is not None else _default_jitter(*jitter_ms)
        self._sleep = sleep

    @property
    def bus(self) -> SessionEventBus:
        """进程内总线（SSE 端点经它订阅；P0 单进程，不引消息队列——A3 §0.5）。"""
        return self._bus

    # ------------------------------------------------------------ 对外唯一入口

    async def emit(self, payload: BaseEvent | Mapping[str, Any]) -> BaseEvent:
        """校验 → 落库（含 seq 分配与冲突重试）→ 广播，返回带真实 seq 的事件。"""
        event = validate_event_payload(payload)
        ensure_p0_event(event)
        session_id = _as_uuid(event.session_id, field="session_id")

        for attempt in range(1, self._max_attempts + 1):
            try:
                seq = await self._insert(event, session_id)
            except IntegrityError as exc:
                if not _is_seq_conflict(exc):
                    raise  # 确定性库错（外键/非空/类型）重试无意义
                if attempt >= self._max_attempts:
                    raise SeqAllocationError(
                        f"seq 分配连续冲突 {attempt} 次（session={session_id}）；"
                        "请检查是否存在异常并发写入"
                    ) from exc
                BusinessLog(
                    message="事件 seq 冲突，抖动后退避重试",
                    action="emit_event",
                    entity="session",
                    entity_id=str(session_id),
                ).warning(exc)
                await self._sleep(self._jitter())
                continue

            stored = event.model_copy(update={"seq": seq})
            self._bus.publish(stored)  # 落库已 COMMIT：广播的是可重放的事实
            return stored

        raise SeqAllocationError(  # pragma: no cover —— 循环内必然 return/raise
            f"seq 分配重试耗尽（session={session_id}）"
        )

    # ------------------------------------------------------------ 内部

    def _seq_expression(self, session_id: uuid.UUID, *, offset: int = 1) -> Any:
        """会话内下一个 seq（`COALESCE(MAX(seq), -1) + offset` 的相关标量子查询）。

        `offset` 只给测试留口子（故意落后一位以确定性触发唯一冲突 → 验证重试路径）；
        生产路径恒为 +1。
        """
        return (
            select(func.coalesce(func.max(EventRow.seq), -1) + offset)
            .where(EventRow.session_id == session_id)
            .scalar_subquery()
        )

    def _insert_statement(self, event: BaseEvent, session_id: uuid.UUID) -> Any:
        """单条 INSERT：seq 由子查询在库内分配，`RETURNING seq` 带回落库值。"""
        return (
            insert(EventRow)
            .values(
                seq=self._seq_expression(session_id),
                session_id=session_id,
                run_id=_optional_uuid(event.run_id),
                agent=_agent_payload(event),
                type=event_type_of(event),
                data=_data_payload(event),
                created_at=event.created_at,
            )
            .returning(EventRow.seq)
        )

    async def _insert(self, event: BaseEvent, session_id: uuid.UUID) -> int:
        """独立短事务落库：本函数返回即已 COMMIT（先落库后广播的「落库」半步）。

        失败路径显式 rollback 后再把异常交给重试循环——短事务的连接立即归还连接池，
        重试才真正"退避后重来"而不是抱着一个悬挂事务。
        """
        factory = self._session_factory or get_session_factory()
        async with factory() as session:
            try:
                seq = await session.scalar(self._insert_statement(event, session_id))
            except IntegrityError:
                await session.rollback()
                raise
            await session.commit()
            return int(seq)


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    return _as_uuid(value, field="run_id") if value else None


def _agent_payload(event: BaseEvent) -> dict[str, Any] | None:
    """`agent` 列（jsonb）：AgentRef 快照；None = 主智能体（契约语义）。"""
    return event.agent.model_dump(mode="json") if event.agent is not None else None


def _data_payload(event: BaseEvent) -> dict[str, Any]:
    """`data` 列：扁平事件**去掉信封列**后的载荷（`models/events.py` 口径）。"""
    dumped = event.model_dump(mode="json")
    for field in _ENVELOPE_FIELDS:
        dumped.pop(field, None)
    return dumped
