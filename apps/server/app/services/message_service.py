"""消息域服务（A3 §6.1/§7.3/§8；REQ-001/004；M2 任务 7 流式接口车道的 REST 侧）。

**REST 来源事件时序钉死（A3 §6.1 / §7.3，本文件是三处落点之一）**：

    业务行落库（消息 + run）→ **commit** → `pipeline.emit(message_created)`（独立短事务）
    → 返回 `MessageSendAck(event_seq=水位)`

事件永不引用未提交的业务事实（消除幽灵事件窗口）；emit 失败时业务事实已成立不回滚，
`EventEmitError` 冒泡按 INTERNAL 暴露（P0 不引 outbox）。

空白判定（AC-001-4 的**首位前置检查**）：契约 `MessageCreate` 的 validator 是二道防线——
其失败会落 INVALID_ARGUMENT；为保证契约区分度，空白在 service 层最先判（`MessageEmpty`/422），
随后再过一遍 `MessageCreate` 结构闸门（私货字段/类型错误仍由契约 `extra="forbid"` 拒收）。

历史分页：键集游标（`(created_at, id)` 行值比较，方向 = 最新在前），游标编解码复用
`schemas/internal.Cursor`（契约只定义不透明 `next_cursor` 字符串，结构属服务端内部约定）。
页大小是模块常量（契约该端点只声明 `cursor` 查询参数，不暴露 limit 旋钮）。

分层约定（基座铁律 10）：DTO 进 DTO 出（wire DTO 复用契约包）；只 `flush()` 不 `commit()`
——**唯一例外**是 `post_message` 的业务行 commit（上面钉死的顺序，A3 §7.3 短事务模式变体）。
run 启动经**注入的 `run_starter`** 协作者（api 层由 dependencies 装配为 run_service）——
本模块不 import `app.agent`（A3 §1.2：消息域服务与编排层解耦）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import EventPipeline
from app.exception.common import MessageEmpty
from app.exception.run import RunAlreadyActive
from app.exception.session import SessionNotFound
from app.models import Message, Run, Session
from app.schemas.internal import Cursor, decode_cursor, encode_cursor
from app.services.base import BaseService
from lcode_contracts.events import MessageCreatedEvent
from lcode_contracts.rest import MessageCreate, MessageItem, MessagePage, MessageSendAck

ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "waiting_approval")
"""活动 run 状态（契约 runs.status 五值中的两个非终态；与会话域服务同口径）。"""

MESSAGE_PAGE_SIZE: int = 50
"""历史分页页大小（契约该端点不暴露 limit 旋钮；测试经 monkeypatch 收小验证翻页）。"""


@dataclass(frozen=True, slots=True)
class RunLaunch:
    """一次 run 的启动入参快照（会话行读出的固化值；run_service 据此装配执行单元）。

    `approval_mode` 是 run 启动快照（M3 D5）：本 run 的审批策略固定用它，决策窗口内
    会话模式变化只影响下一条消息；resume 段同样复用该值（避免 when 翻转）。
    """

    session_id: str
    run_id: str
    content: str
    model: str
    reasoning_effort: str
    provider: str
    approval_mode: str = "manual"
    user_id: str = "local"


RunStarter = Callable[[RunLaunch], None]
"""run 启动器：`RunLaunch → None`——同步调度后台任务、不阻塞 ack（api 层装配为 run_service）。"""


class MessageService(BaseService):
    """消息域业务逻辑：发送（落库→commit→emit→ack）与历史（键集游标分页）。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        pipeline: EventPipeline,
        run_starter: RunStarter | None = None,
    ) -> None:
        super().__init__(db)
        self._pipeline = pipeline
        self._run_starter = run_starter

    # ---------------------------------------------------------------- 发送

    async def post_message(self, session_id: str, content: str) -> MessageSendAck:
        """发送用户消息：落库 → commit → emit → ack（A3 §6.1 的钉死顺序）。

        错误路径（契约端点 errors 声明序）：空白 → `MESSAGE_EMPTY`(422)；会话不存在 →
        `SESSION_NOT_FOUND`(404)；已有活动 run → `RUN_ALREADY_ACTIVE`(409)。
        """
        self._require_non_blank(content)  # ① 首位前置检查（契约区分度，AC-001-4）
        MessageCreate.model_validate({"content": content})  # ② 二道防线：结构闸门
        session_row = await self._require_session(session_id)  # ③
        await self._require_no_active_run(session_row.id)  # ④

        run = Run(session_id=session_row.id, status="running", started_at=datetime.now(UTC))
        self.db.add(run)
        await self.db.flush()  # 先拿 run.id（server_default gen_random_uuid() 经 RETURNING 回填）
        message = Message(
            user_id=session_row.user_id,
            session_id=session_row.id,
            run_id=run.id,
            role="user",
            content=content,
        )
        self.db.add(message)
        session_row.last_active_at = datetime.now(UTC)  # 会话活跃推进（AC-004-3 排序依据）
        await self.db.flush()

        # ⑤ 业务事实先提交（短事务模式变体，A3 §7.3）：emit 与 ack 都建立在已提交事实上
        await self.db.commit()

        # ⑥ REST 来源事件：commit 之后才 emit（消除幽灵事件窗口）
        now = datetime.now(UTC)
        stored = await self._pipeline.emit(
            MessageCreatedEvent(
                seq=0,  # 真实 seq 由 EventPipeline 统一分配（MAX+1，唯一索引兜底）
                session_id=str(session_row.id),
                run_id=str(run.id),
                agent=None,  # 用户消息恒为主智能体视角
                created_at=now,
                message_id=str(message.id),
                role="user",
                content=content,
            )
        )

        # ⑦ 启动后台 run（同步调度不阻塞 ack；事件已落库，run 的输出经 emitter 续流）
        if self._run_starter is not None:
            self._run_starter(
                RunLaunch(
                    session_id=str(session_row.id),
                    run_id=str(run.id),
                    content=content,
                    model=session_row.model,
                    reasoning_effort=session_row.reasoning_effort,
                    provider=session_row.sandbox_provider,
                    approval_mode=session_row.approval_mode,
                    user_id=session_row.user_id,
                )
            )

        return MessageSendAck(message_id=str(message.id), run_id=str(run.id), event_seq=stored.seq)

    # ---------------------------------------------------------------- 历史

    async def list_messages(self, session_id: str, *, cursor: str | None = None) -> MessagePage:
        """消息历史（导出/检索用；面板重建走事件流）：键集游标分页，最新在前。"""
        session_row = await self._require_session(session_id)
        statement = select(Message).where(Message.session_id == session_row.id)
        if cursor is not None:
            watermark = decode_cursor(cursor)  # 非法游标 → InvalidArgument（422）
            # 行值比较（keyset）：`(created_at, id) < 水位`，SQLAlchemy 生成参数化 ROW 比较；
            # mypy 不识别 Tuple 构造的运算符重载（SQLAlchemy 2.0 的已知标注缺口）→ 定点豁免
            statement = statement.where(
                tuple_(Message.created_at, Message.id)
                < (watermark.created_at, uuid.UUID(watermark.id))
            )
        statement = (
            statement.order_by(Message.created_at.desc(), Message.id.desc()).limit(
                MESSAGE_PAGE_SIZE + 1
            )  # 多取一行判尾页（keyset 标准做法）
        )
        rows = list(await self.db.scalars(statement))

        next_cursor: str | None = None
        if len(rows) > MESSAGE_PAGE_SIZE:
            rows = rows[:MESSAGE_PAGE_SIZE]
            last = rows[-1]
            next_cursor = encode_cursor(Cursor(created_at=last.created_at, id=str(last.id)))
        return MessagePage(items=[self._to_item(row) for row in rows], next_cursor=next_cursor)

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _require_non_blank(content: str) -> None:
        if not content.strip():
            raise MessageEmpty("消息内容不能为空", detail="空白（含纯空白字符）消息不触发 run")

    async def _require_session(self, session_id: str) -> Session:
        """按 wire id 取会话行；非法 UUID 与不存在同口径 → `SESSION_NOT_FOUND`。"""
        try:
            key = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFound(
                f"会话不存在：{session_id}", detail="路径参数不是合法 UUID", original_exception=exc
            ) from exc
        row = await self.db.scalar(select(Session).where(Session.id == key))
        if row is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        return row

    async def _require_no_active_run(self, session_id: uuid.UUID) -> None:
        """会话级串行（P0）：已有活动 run 再发消息 → `RUN_ALREADY_ACTIVE`（409）。"""
        active = await self.db.scalar(
            select(Run.id).where(Run.session_id == session_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        )
        if active is not None:
            raise RunAlreadyActive("该会话已有运行中的任务", detail=f"active_run={active}")

    @staticmethod
    def _to_item(row: Message) -> MessageItem:
        """ORM 行 → 契约 `MessageItem` 投影（不含 thinking；ORM 不逃出 service 层）。"""
        return MessageItem.model_validate(
            {
                "id": str(row.id),
                "role": row.role,
                "content": row.content,
                "agent_id": row.agent_id,
                "run_id": str(row.run_id) if row.run_id is not None else None,
                "created_at": row.created_at,
                "finish_reason": row.finish_reason,
                "structured": row.structured,
            }
        )


__all__ = ["ACTIVE_RUN_STATUSES", "MESSAGE_PAGE_SIZE", "MessageService", "RunLaunch", "RunStarter"]
