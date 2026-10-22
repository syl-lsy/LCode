"""审批域服务（M3 Phase 1，REQ-006/013）：pending 批、列表/详情、四决策、AUTO 留痕、stop 清理。

职责边界（A3 §7.3 事务口径）：

- **后台 run 侧**（`create_pending_batch` / `record_auto_approval`）：run 不是请求，
  自取短会话立即 commit（`core/db.get_session_factory`），不占请求连接；
- **请求侧**（`list_approvals` / `get_approval` / `decide` / `close_pending_for_run`）：
  用注入的请求级 `db`，只 flush；但 `decide` 与 `close_pending_for_run` 必须在
  emit 前显式 commit——事件永不引用未提交的业务事实（A3 §6.1 钉死顺序），
  `message_service.post_message` 是同款例外落点。

审批是**唯一闸门**（AGENTS.md 硬约束 11）：本服务只负责把人工/分类器决策落行并
触发 resume；它不构造任何"自动放行"路径——没有决策就 resume 不了，决策不齐时
HITL middleware 会因数量不符抛错（run 以 error 收敛，fail-closed）。

`scope=session`（PRD L307）：P0 的"完全访问/持续批准"只升会话级 approval_mode=auto
（规则持久化 API 归 P1），且**只允许与 approve/edit 组合**——reject/respond 带 session
会被 `_validate_decision` 以 `INVALID_ARGUMENT` 拒绝（否则"拒绝并始终拒绝"会变成
"拒绝并放开自动审批"）。AUTO 也不放行沙箱红线工具（permissions.SANDBOX_REDLINE_TOOLS
恒在 interrupt 面），所以该升级不会自动放行任何命令执行/文件写。

AUTO 留痕接口 `record_auto_approval` / `record_auto_approval_short_tx` 是 Phase 3
分类器的落行入口：AUTO 分类器判定免审时，`run_service` 以短事务回调把决策落成
`status=auto_approved` / `decided_by=classifier` / `decision=None` / `reason=分类器理由`
的一行（AC-013-3 审计面）。落行按 `(session_id, run_id, tool_call_id)` 幂等——resume
重放同一 tool_call 不会重复落行。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events import EventPipeline
from app.exception.approval import ApprovalAlreadyResolved, ApprovalNotFound
from app.exception.common import InvalidArgument
from app.models import Approval, Session
from app.models import Run as RunRow
from app.schemas.internal import Cursor, decode_cursor, encode_cursor
from app.services.base import BaseService
from lcode_contracts.errors import RunErrorObj
from lcode_contracts.events import ApprovalResolvedEvent, RunStatusEvent
from lcode_contracts.rest import ApprovalDecisionRequest, ApprovalItem, ApprovalPage

APPROVAL_PAGE_SIZE: int = 50
"""审批列表页大小（与会话/消息列表同量级；契约端点不暴露 limit 旋钮）。"""

RESUME_FAILED_ERROR_CODE: str = "internal"
"""resume 启动失败收敛 run 时写的事件侧错误码（契约 RunErrorCode 的兜底值）。"""

RESUME_FAILED_MESSAGE: str = "审批恢复启动失败，请重新发送消息重试"
"""resume 启动失败的用户可读文案（进 run 行 error_message 与 run_status(error)）。

`retryable` 置 True 的语义：本仓 UI 的该标记含义是「重新发送即可」（RunErrorBanner
tooltip）——这是本失败态唯一且真实的恢复路径（P0 无独立 resume 恢复端点）。"""

ApprovalStatus = Literal["pending", "approved", "rejected", "edited", "responded", "auto_approved"]
"""approvals.status 值域（契约 `ApprovalItem.status` 同源）。"""

_DECISION_TO_STATUS: dict[str, str] = {
    "approve": "approved",
    "edit": "edited",
    "reject": "rejected",
    "respond": "responded",
}
"""四决策 → 行状态（AC-006-1~4 的留痕映射）。"""

_HITL_DECISION_LITERALS: dict[str, str] = {
    "approve": "approve",
    "edit": "edit",
    "reject": "reject",
    "respond": "respond",
}
"""决策 → 安装版 `HumanInTheLoopMiddleware` 的 resume 字面量（U3 对表；当前同名直映）。"""

DECIDED_BY_USER: str = "user"
"""人工决策的 decided_by 值（AUTO 分类器为 classifier，stop 清理为 system）。"""

DECIDED_BY_SYSTEM: str = "system"
DECIDED_BY_CLASSIFIER: str = "classifier"


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """一条待审批工具调用（translator.ApprovalTarget 的服务侧输入投影）。"""

    tool_call_id: str
    tool: str
    args: dict[str, Any]
    risk_level: Literal["medium", "high"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """一条 resume 决策（service → run_service → HITL `Command(resume=...)`）。

    `tool` 只在 edit 决策里被消费（HITL `edited_action.name` 必须是原工具名——
    契约 `edited_args` 只携带参数，不允许改工具）。
    """

    tool_call_id: str
    decision: Literal["approve", "edit", "reject", "respond"]
    tool: str = ""
    edited_args: dict[str, Any] | None = None
    feedback: str | None = None

    def to_hitl_decision(self) -> dict[str, Any]:
        """契约决策 → 安装版 HITL resume 载荷（`{"decisions": [本形态, ...]}` 的元素）。"""
        literal = _HITL_DECISION_LITERALS[self.decision]
        if self.decision == "edit":
            return {
                "type": literal,
                "edited_action": {"name": self.tool, "args": dict(self.edited_args or {})},
            }
        if self.decision in ("reject", "respond"):
            payload: dict[str, Any] = {"type": literal}
            if self.feedback:
                payload["message"] = self.feedback
            return payload
        return {"type": literal}


ResumeStarter = Callable[[str, list[ResumeDecision]], None]
"""resume 启动器：`(run_id, decisions) → None`（同步调度后台任务、不阻塞决策 ack）。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def create_pending_batch_short_tx(
    *,
    session_id: str,
    run_id: str | None,
    user_id: str,
    items: Sequence[PendingApproval],
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> list[ApprovalItem]:
    """短事务写 pending 批（后台 run 侧入口；一事务 commit 后返回 DTO）。

    顺序保证：`created_at` 按入参序递增微秒——同一批的审批行在列表/resume 决策
    顺序里稳定可排（D3「按 created_at 递增微秒排序」），不依赖数据库时钟精度。
    """
    if not items:
        return []
    factory = session_factory or get_session_factory()
    base = now()
    rows: list[Approval] = []
    async with factory() as session:
        for offset, item in enumerate(items):
            rows.append(
                Approval(
                    user_id=user_id,
                    session_id=uuid.UUID(session_id),
                    run_id=uuid.UUID(run_id) if run_id else None,
                    tool_call_id=item.tool_call_id,
                    tool=item.tool,
                    args=dict(item.args),
                    risk_level=item.risk_level,
                    reason=item.reason,
                    status="pending",
                    scope="once",
                    created_at=base + timedelta(microseconds=offset),
                )
            )
            session.add(rows[-1])
        await session.commit()  # 事件出口只引用已提交事实（run_service 随后 emit）
    return [_approval_item(row) for row in rows]


async def record_auto_approval_short_tx(
    *,
    session_id: str,
    run_id: str | None,
    user_id: str,
    item: PendingApproval,
    reason: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> ApprovalItem:
    """短事务写一条 AUTO 分类器决策行（AC-013-3 审计面；按 tool_call_id 幂等）。

    幂等键：`(session_id, run_id, tool_call_id, status='auto_approved')`——resume 重放
    同一工具调用时直接返回既有行，不重复落审计。DB 侧无唯一约束（契约冻结，不改
    索引），单进程单 run 的判重在此足够；并发写入者由 `run_service` 的单一路径保证
    不重叠。
    """
    factory = session_factory or get_session_factory()
    stamp = now()
    session_uuid = uuid.UUID(session_id)
    run_uuid = uuid.UUID(run_id) if run_id else None
    row = Approval(
        user_id=user_id,
        session_id=session_uuid,
        run_id=run_uuid,
        tool_call_id=item.tool_call_id,
        tool=item.tool,
        args=dict(item.args),
        risk_level=item.risk_level,
        reason=reason or item.reason,
        status="auto_approved",
        scope="once",
        decided_by=DECIDED_BY_CLASSIFIER,
        decided_at=stamp,
        created_at=stamp,
    )
    async with factory() as session:
        existed = await session.scalar(
            select(Approval)
            .where(
                Approval.session_id == session_uuid,
                Approval.tool_call_id == item.tool_call_id,
                Approval.status == "auto_approved",
                Approval.run_id == run_uuid if run_uuid is not None else Approval.run_id.is_(None),
            )
            .order_by(Approval.created_at.asc())
            .limit(1)
        )
        if existed is not None:
            return _approval_item(existed)
        session.add(row)
        await session.commit()
    return _approval_item(row)


class ApprovalService(BaseService):
    """审批域业务：pending 批 / 列表 / 详情 / 四决策 / AUTO 留痕 / stop 清理。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        pipeline: EventPipeline,
        resume_starter: ResumeStarter | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(db)
        self._pipeline = pipeline
        self._resume_starter = resume_starter
        self._session_factory = session_factory
        self._now = now

    # ---------------------------------------------------------------- 后台 run 侧

    async def create_pending_batch(
        self,
        *,
        session_id: str,
        run_id: str | None,
        user_id: str,
        items: Sequence[PendingApproval],
    ) -> list[ApprovalItem]:
        """一事务写 N 行 pending（REQ-006/AC-006-5），commit 后返回 DTO（短事务口径）。"""
        return await create_pending_batch_short_tx(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            items=items,
            session_factory=self._session_factory,
            now=self._now,
        )

    async def record_auto_approval(
        self,
        *,
        session_id: str,
        run_id: str | None,
        user_id: str,
        item: PendingApproval,
        reason: str | None = None,
    ) -> ApprovalItem:
        """AUTO 分类器决策落行（AC-013-3 审计面；短事务 + 按 tool_call_id 幂等）。

        落 `status=auto_approved`、`decided_by=classifier`、`decision=None`（没有人工
        决策字面量）、`reason=分类器理由`；不触发 resume（分类器在 model-call 后置钩子
        内联决策，工具继续执行）。生产路径由 `run_service` 注入的回调直接调模块级
        `record_auto_approval_short_tx`；本方法是 service 形态的同款入口。
        """
        return await record_auto_approval_short_tx(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            item=item,
            reason=reason,
            session_factory=self._session_factory,
            now=self._now,
        )

    # ---------------------------------------------------------------- 查询

    async def list_approvals(
        self,
        *,
        status: ApprovalStatus | str | None = None,
        session_id: str | None = None,
        cursor: str | None = None,
    ) -> ApprovalPage:
        """审批列表（AC-006-5 刷新恢复：`status=pending` + `session_id`）。

        键集游标分页（`(created_at, id)` 降序，最新在前）——与会话消息列表同口径；
        契约只把 `next_cursor` 定义为不透明字符串。
        """
        statement = select(Approval)
        if status is not None:
            statement = statement.where(Approval.status == str(status))
        if session_id is not None:
            statement = statement.where(Approval.session_id == self._uuid_of(session_id))
        if cursor is not None:
            watermark = decode_cursor(cursor)
            statement = statement.where(
                tuple_(Approval.created_at, Approval.id)
                < (watermark.created_at, uuid.UUID(watermark.id))
            )
        statement = statement.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(
            APPROVAL_PAGE_SIZE + 1
        )
        rows = list(await self.db.scalars(statement))

        next_cursor: str | None = None
        if len(rows) > APPROVAL_PAGE_SIZE:
            rows = rows[:APPROVAL_PAGE_SIZE]
            last = rows[-1]
            next_cursor = encode_cursor(Cursor(created_at=last.created_at, id=str(last.id)))
        return ApprovalPage(items=[_approval_item(row) for row in rows], next_cursor=next_cursor)

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        """审批详情（404 `APPROVAL_NOT_FOUND`）。"""
        return _approval_item(await self._require_row(approval_id))

    # ---------------------------------------------------------------- 四决策

    async def decide(
        self, approval_id: str, payload: ApprovalDecisionRequest
    ) -> tuple[ApprovalItem, str | None]:
        """落一次决策（AC-006-1~4）：锁内判定 → commit → 发 approval_resolved → 触发 resume。

        返回 `(审批行投影, run_id)`：`run_id` 非空 = 该审批所属 run（供 ack 引用；
        run 行已不存在时为 None）。

        幂等保护：非 pending 行重复决策 → `APPROVAL_ALREADY_RESOLVED`(409)；未知 id →
        `APPROVAL_NOT_FOUND`(404)。edit 必须带**非空** edited_args、respond 必须带非空
        feedback、`scope=session` 只允许与 approve/edit 组合（契约 DTO 已校验结构，
        这里再守语义一道 → `INVALID_ARGUMENT`）。

        并发收口（行级锁，随本次 commit 释放；不引分布式机制——P0 单进程单用户）：
        - 审批行 `FOR UPDATE`：同一审批的重复/并发决策串行化，后到者重读到已决状态 →
          409（不会双落行、双发 approval_resolved）；也与 stop 的 pending 锁定读串行化
          （见 `close_pending_for_run`，安全审计 P2-1）；
        - 所属 run 行 `FOR UPDATE`：同一 run 的多条并发决策（同批多个审批同时点）在
          「落行 + 全部已决判定」阶段串行化——既防止两边都看到对方仍是 pending 而
          **都**判定「未齐备」（漏唤醒，run 永久卡在 waiting_approval），也防止两边
          都看到完整决策集而**双双**调度 resume。锁顺序恒为 approvals → runs（与
          stop/删除联动的子表顺序同向，无反向加锁路径）。
        - `scope=session` 的会话升级走**独立短事务**（`_upgrade_session_scope`）：
          决策主事务若也改 sessions 行，锁序会变成 approvals → runs → sessions，与
          删除会话的 sessions → approvals → runs 成环（互等死锁）。
        - 决策回声（`approval_resolved`）故障**不阻断续跑**：决策行已提交，emit 抛异常
          若冒泡出去，客户端既拿不到回声、也等不到 resume（run 永久停在
          waiting_approval）。顺序仍是「先回声后 resume」（产品语义），只把 emit 失败
          降级为 ERROR 日志。
        - 其余**commit 之后的副作用**同款隔离（安全审计同族根因）：`scope=session` 的
          会话升级失败、resume 启动失败都不得改写「决策成功」这一事实（不 500），
          失败只记 ERROR；其中 resume 启动失败还会把 run 收敛成 `error`（见
          `_fail_run_after_resume_error`），避免会话被永久占在活动 run 状态。
        """
        row = await self._require_pending_row(approval_id)
        if row.status != "pending":
            raise ApprovalAlreadyResolved(
                "该审批已处理", detail=f"approval={approval_id} status={row.status}"
            )
        self._validate_decision(payload)

        now = self._now()
        row.status = _DECISION_TO_STATUS[payload.decision]
        row.decision = payload.decision
        row.scope = payload.scope
        row.edited_args = dict(payload.edited_args) if payload.edited_args is not None else None
        row.feedback = payload.feedback
        row.decided_by = DECIDED_BY_USER
        row.decided_at = now

        await self._lock_run_for_decision(row.run_id)
        await self.db.flush()
        # 「全部已决」判定必须在**同一把 run 行锁内**完成：若放到 commit 之后，
        # 两条并发的批次决策可能各自在提交后都看到完整决策集、双双调度 resume
        # （只剩进程内 claim 兜底）。锁内判定则只有「补齐最后一条」的事务能通过——
        # 另一条此时尚未提交，仍被本事务视为 pending。
        decisions = await self._resume_decisions_if_complete(row)
        await self.db.commit()  # 事件只引用已提交事实（A3 §6.1）

        if payload.scope == "session":
            # 会话升级是决策**已提交之后**的副作用：失败不得改写决策成败（不 500），
            # 否则客户端以为决策失败、实际已落库，重试命中 409。只记可审计的 ERROR。
            try:
                await self._upgrade_session_scope(row.session_id)
            except Exception as exc:  # noqa: BLE001 —— 副作用故障不得改写决策结果
                BusinessLog(
                    message="审批决策已落库，但会话级升级失败（approval_mode 未变更）",
                    action="升级会话审批范围",
                    entity="session",
                    entity_id=str(row.session_id),
                ).error(exc)

        # 回声先于 resume（产品语义：决策回声先于续跑产生的后续事件）；但 emit 失败
        # 绝不能吞掉续跑——决策行已 committed，冒泡会让 run 永久卡在 waiting_approval
        # （客户端无回声、也无 resume）。失败只记 ERROR，续跑照旧执行。
        try:
            await self._emit_resolved(row, performed_by=DECIDED_BY_USER)
        except Exception as exc:  # noqa: BLE001 —— 事件出口故障不得阻断续跑
            BusinessLog(
                message="审批决策回声发射失败（决策已落库，继续触发续跑）",
                action="决策审批",
                entity="approval",
                entity_id=str(row.id),
            ).error(exc)

        run_id = str(row.run_id) if row.run_id is not None else None
        if decisions is not None and run_id is not None:
            try:
                await self._start_resume(run_id, decisions)
            except Exception as exc:  # noqa: BLE001 —— 续跑启动失败必须收敛 run，不能 500
                BusinessLog(
                    message="审批恢复启动失败（run 收敛为 error，可重新发送消息）",
                    action="恢复审批运行",
                    entity="run",
                    entity_id=run_id,
                ).error(exc)
                await self._fail_run_after_resume_error(run_id)
        return _approval_item(row), run_id

    async def close_pending_for_run(self, run_id: str, *, feedback: str) -> list[str]:
        """stop 语义（D3）：该 run 的全部 pending 置 rejected(decided_by=system) 并广播。

        返回被清理的审批 id 列表。事件逐条 emit（多标签页一致，AC-008-3）；重复调用
        幂等（已无 pending 行时返回空列表）。

        **与 decide 的并发收口**（安全审计 P2-1）：pending 读走 `SELECT ... FOR UPDATE`，
        与 decide 的 `_require_pending_row` 共用同一把审批行锁：

        - stop 先拿到锁 → decide 后到重读已决 → 409（用户决策不会被 system 覆盖）；
        - decide 先拿到锁 → stop 阻塞到其 commit，READ COMMITTED 的 EvalPlanQual 按
          `status='pending'` 重判并把已决行过滤掉（不会把 approved(user) 改成
          rejected(system)，也不发矛盾的 system 回声）。

        锁序：stop 只锁 approvals（按 created_at/id 升序），**不锁 runs**；decide 是
        approvals → runs——两者同向，不成环。

        行级守卫（第二道，防替身/未来新读路径）：即使读到的行已被并发解决，也跳过且
        不回声——已解决的决策回声不得被 system 覆盖。
        """
        pending = await self._pending_for_run(self._uuid_of(run_id), for_update=True)
        now = self._now()
        closed: list[Approval] = []
        for row in pending:
            if row.status != "pending":  # 已被用户决策抢先：不覆盖、不回声
                continue
            row.status = "rejected"
            row.decision = "reject"
            row.scope = "once"
            row.feedback = feedback
            row.decided_by = DECIDED_BY_SYSTEM
            row.decided_at = now
            closed.append(row)
        if not closed:
            return []
        await self.db.flush()
        await self.db.commit()
        # 逐条隔离：某条回声发射失败不得让其后的卡片漏回声（库里都已是 rejected，
        # 但其它标签页只靠事件同步）。失败逐条记 ERROR。
        for row in closed:
            try:
                await self._emit_resolved(row, performed_by=DECIDED_BY_SYSTEM)
            except Exception as exc:  # noqa: BLE001 —— 单条回声故障不拖累其余条
                BusinessLog(
                    message="停止清理的审批回声发射失败（其余卡片不受影响）",
                    action="关闭审批",
                    entity="approval",
                    entity_id=str(row.id),
                ).error(exc)
        return [str(row.id) for row in closed]

    # ---------------------------------------------------------------- 内部

    async def _require_row(self, approval_id: str) -> Approval:
        key = self._uuid_of(approval_id, kind="审批")
        row = await self.db.get(Approval, key)
        if row is None:
            raise ApprovalNotFound(f"审批不存在：{approval_id}")
        return row

    async def _require_pending_row(self, approval_id: str) -> Approval:
        """决策入口：取审批行并加行级锁（`SELECT ... FOR UPDATE`）。

        锁随本次决策 commit 释放；并发的同审批决策在后到者重读时已看到 `approved`，
        直接走 409 分支（不会双落行/双回声）。读用 `populate_existing` 语义（`get` +
        `with_for_update`）：即使会话内已有该行缓存，也强制走一次锁定读。
        """
        key = self._uuid_of(approval_id, kind="审批")
        row = await self.db.get(Approval, key, with_for_update=True, populate_existing=True)
        if row is None:
            raise ApprovalNotFound(f"审批不存在：{approval_id}")
        return row

    async def _upgrade_session_scope(self, session_id: uuid.UUID) -> None:
        """`scope=session` 的会话级升级（独立短事务；锁序见 `decide` docstring）。

        P0「完全访问/持续批准」= approval_mode=auto（PRD L307）；沙箱红线工具不受影响
        （恒在 interrupt 面，AC-013-4）。升级晚于决策 committed 一步不影响本轮审批——
        run 的 approval_mode 是**启动快照**（D5，决策窗口内翻转不生效）。
        """
        factory = self._session_factory or get_session_factory()
        async with factory() as session:
            session_row = await session.get(Session, session_id)
            if session_row is None:  # 会话已删：决策已落行，无升级对象
                return
            session_row.approval_mode = "auto"
            await session.commit()

    async def _lock_run_for_decision(self, run_id: uuid.UUID | None) -> None:
        """锁住决策所属 run 行（`SELECT ... FOR UPDATE`，随本次 commit 释放）。

        作用：同一 run 的并发决策在「落行 → 判断是否全部已决」阶段串行化——后到者的
        未决判断必然发生在前一决策 commit 之后，因此不可能出现「两边都看到对方
        pending、谁都不 resume」的漏唤醒。run 行不存在（会话已删）时不阻塞、不抛错：
        `_start_resume` 会按缺失行跳过。
        """
        if run_id is None:
            return
        await self.db.get(RunRow, run_id, with_for_update=True, populate_existing=True)

    @staticmethod
    def _validate_decision(payload: ApprovalDecisionRequest) -> None:
        """契约 DTO 之外的语义守门（fail-closed：非法决策不落行、不升级会话）。

        1. `scope=session` 只允许与 approve/edit 组合（PRD L307「持续批准」语义）：
           reject/respond 携带 session 会把 `sessions.approval_mode` 升到 auto——
           「拒绝并始终拒绝」被实现成「拒绝并放开自动审批」（安全审计 P2-2）；
        2. edit 必须带**非空** edited_args：空对象等于 approve，却留下「用户编辑过」的
           审计痕迹（code review P3）；
        3. respond 必须带非空 feedback。
        """
        if payload.scope == "session" and payload.decision not in ("approve", "edit"):
            raise InvalidArgument(
                "scope=session 只能与 approve/edit 组合",
                detail=f"decision={payload.decision} 携带 scope=session 会误升会话审批模式",
            )
        if payload.decision == "edit":
            if not isinstance(payload.edited_args, Mapping):
                raise InvalidArgument(
                    "decision=edit 时必须携带 edited_args", detail="edited_args 缺失或不是对象"
                )
            if not payload.edited_args:
                raise InvalidArgument(
                    "decision=edit 时 edited_args 不能为空对象",
                    detail="空 edited_args 等同 approve，但会留下被编辑的审计痕迹",
                )
        if payload.decision == "respond" and not (payload.feedback or "").strip():
            raise InvalidArgument(
                "decision=respond 时必须携带非空 feedback", detail="feedback 为空"
            )

    async def _pending_for_run(
        self, run_id: uuid.UUID, *, for_update: bool = False
    ) -> list[Approval]:
        """该 run 的 pending 行（按 created_at/id 升序：批次内的稳定顺序）。

        `for_update=True` **只给 stop 清理用**（与 decide 的审批行锁串行化，见
        `close_pending_for_run`）。decide 的「全部已决」判定必须用普通读：它此时已持有
        本批目标行 + run 行的锁，若再按序锁整批 pending 行，锁序会变成
        approvals(target) → runs → approvals(其余)，与同批另一条并发决策
        （approvals(另一行) → runs）互等成环（PostgreSQL 判 DeadlockDetected）。
        """
        statement = (
            select(Approval)
            .where(Approval.run_id == run_id, Approval.status == "pending")
            .order_by(Approval.created_at.asc(), Approval.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        rows = await self.db.scalars(statement)
        return list(rows)

    async def _rows_for_run(self, run_id: uuid.UUID) -> list[Approval]:
        """该 run 的全部审批行（升序；resume 决策列表的数据源）。"""
        rows = await self.db.scalars(
            select(Approval)
            .where(Approval.run_id == run_id)
            .order_by(Approval.created_at.asc(), Approval.id.asc())
        )
        return list(rows)

    async def _resume_decisions_if_complete(self, decided: Approval) -> list[ResumeDecision] | None:
        """同 run 全部 pending 已解决 → 按顺序返回 resume 决策列表；否则 None。"""
        if decided.run_id is None:
            return None
        if await self._pending_for_run(decided.run_id):
            return None
        rows = await self._rows_for_run(decided.run_id)
        decisions: list[ResumeDecision] = []
        for row in rows:
            if row.decision is None:
                # auto_approved 行没有人工决策字面量（分类器决策；混进 resume 会让
                # HITL 数量不符 → fail-closed）。AUTO 模式下这是**预期行**，静默跳过；
                # 其它状态缺决策字面量属异常，记审计 warning。
                if row.status != "auto_approved":
                    BusinessLog(
                        message="审批行已解决但无决策字面量（跳过 resume 决策）",
                        action="组装审批决策",
                        entity="approval",
                        entity_id=str(row.id),
                    ).warning()
                continue
            decisions.append(self._to_decision(row))
        return decisions

    @staticmethod
    def _to_decision(row: Approval) -> ResumeDecision:
        """审批行 → resume 决策（edit/reject/respond 的载荷从行里回放）。"""
        decision = row.decision
        if decision not in ("approve", "edit", "reject", "respond"):  # pragma: no cover
            raise InvalidArgument("审批行的决策字面量非法", detail=f"decision={decision!r}")
        return ResumeDecision(
            tool_call_id=row.tool_call_id,
            decision=decision,  # type: ignore[arg-type]  # 上一行已收敛为四字面量
            tool=row.tool,
            edited_args=dict(row.edited_args) if row.edited_args is not None else None,
            feedback=row.feedback,
        )

    async def _start_resume(self, run_id: str, decisions: list[ResumeDecision]) -> None:
        """触发 resume（同 run_id/thread_id 续跑）；run 已非等待态时只记日志不 resume。

        守卫读**强制刷新**（`populate_existing`）：run 行在本请求的决策锁阶段已进
        identity map，直接 `get` 会命中 commit 前的陈旧快照（`expire_on_commit=False`）；
        并发 stop 在决策 commit 之后收口时，必须按库里的真实终态跳过 resume。
        """
        run_row = await self.db.get(RunRow, uuid.UUID(run_id), populate_existing=True)
        if run_row is None:
            BusinessLog(
                message="审批齐备但 run 行已不存在，跳过 resume",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).warning()
            return
        if run_row.status != "waiting_approval":
            BusinessLog(
                message="审批齐备但 run 不在等待审批态，跳过 resume",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).warning()
            return
        if self._resume_starter is None:  # pragma: no cover —— 生产由 API 装配注入
            BusinessLog(
                message="未注入 resume 启动器（审批已落行，run 不会续跑）",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).warning()
            return
        self._resume_starter(run_id, decisions)

    async def _fail_run_after_resume_error(self, run_id: str) -> None:
        """resume 启动失败的兜底收口：`waiting_approval` → `error`（并广播 `run_status(error)`）。

        **为什么必须收敛（而不是留在 waiting_approval）**：此刻审批行已终态，用户重试
        decide 只会命中 409；P0 也没有独立的 resume 恢复端点。留在 `waiting_approval`
        会让会话永久处于「有活动 run」状态——新消息撞 `RUN_ALREADY_ACTIVE`、前端一直
        显示等待审批却没有可决策卡片，用户没有任何自助恢复手段。`error` 是契约五值里
        唯一诚实且可恢复的终态：`error_message` 明确告知「重新发送消息重试」，
        `run_status(error)` 让横幅立即出现，`run_state` 随之回到 idle。

        条件迁移（`status='waiting_approval'`）：并发 stop/其他收口抢先时不得覆盖终态
        （终态归抢先方）。本方法自身失败只记 ERROR——决策 ack 已成功，不能再抛 500。
        """
        try:
            key = uuid.UUID(run_id)
            factory = self._session_factory or get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    update(RunRow)
                    .where(RunRow.id == key, RunRow.status == "waiting_approval")
                    .values(
                        status="error",
                        finished_at=self._now(),
                        error_code=RESUME_FAILED_ERROR_CODE,
                        error_message=RESUME_FAILED_MESSAGE,
                    )
                    .returning(RunRow.session_id)
                )
                session_id = result.scalar_one_or_none()
                if session_id is None:
                    await session.rollback()
                    BusinessLog(
                        message="resume 启动失败但 run 已不在 waiting_approval（终态归抢先方）",
                        action="恢复审批运行",
                        entity="run",
                        entity_id=run_id,
                    ).warning()
                    return
                await session.commit()
            await self._pipeline.emit(
                RunStatusEvent(
                    seq=0,
                    session_id=str(session_id),
                    run_id=run_id,
                    agent=None,
                    created_at=self._now(),
                    subject="run",
                    phase="error",
                    error=RunErrorObj(
                        code=RESUME_FAILED_ERROR_CODE,  # type: ignore[arg-type]  # 常量取自 RunErrorCode
                        message=RESUME_FAILED_MESSAGE,
                        retryable=True,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底本身失败只留痕（不能再抛 500）
            BusinessLog(
                message="resume 启动失败的兜底收口失败（run 可能仍停在 waiting_approval）",
                action="恢复审批运行",
                entity="run",
                entity_id=run_id,
            ).error(exc)

    async def _emit_resolved(self, row: Approval, *, performed_by: str) -> None:
        """`approval_resolved` 事件（决策回声；多标签页与回放重建的唯一信号）。"""
        decision = row.decision or "reject"
        await self._pipeline.emit(
            ApprovalResolvedEvent(
                seq=0,
                session_id=str(row.session_id),
                run_id=str(row.run_id) if row.run_id is not None else None,
                agent=None,
                created_at=self._now(),
                approval_id=str(row.id),
                decision=decision,  # type: ignore[arg-type]  # 行值域由 CHECK + 本服务写入口保证
                scope=row.scope,  # type: ignore[arg-type]  # 同上
                edited_args=row.edited_args,
                feedback=row.feedback,
                decided_by=performed_by,
            )
        )

    @staticmethod
    def _uuid_of(value: str, *, kind: str = "会话") -> uuid.UUID:
        """wire id → UUID；非法即 `INVALID_ARGUMENT`（契约端点错误声明之外的最小暴露）。"""
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise InvalidArgument(
                f"{kind} id 非法：{value}", detail="不是合法 UUID", original_exception=exc
            ) from exc


def _approval_item(row: Approval) -> ApprovalItem:
    """ORM 行 → 契约 `ApprovalItem`（wire id 为 UUID 字符串；列表不投影决策载荷）。"""
    return ApprovalItem.model_validate(
        {
            "id": str(row.id),
            "session_id": str(row.session_id),
            "tool": row.tool,
            "args": row.args,
            "risk_level": row.risk_level,
            "status": row.status,
            "decision": row.decision,
            "scope": row.scope,
            "reason": row.reason,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
        }
    )


__all__ = [
    "APPROVAL_PAGE_SIZE",
    "ApprovalService",
    "ApprovalStatus",
    "DECIDED_BY_CLASSIFIER",
    "DECIDED_BY_SYSTEM",
    "DECIDED_BY_USER",
    "PendingApproval",
    "RESUME_FAILED_ERROR_CODE",
    "RESUME_FAILED_MESSAGE",
    "ResumeDecision",
    "ResumeStarter",
    "create_pending_batch_short_tx",
    "record_auto_approval_short_tx",
]
