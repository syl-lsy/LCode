"""approval_service 单测（m3-plan Phase 1 E；REQ-006 / AC-006-1~5）。

覆盖点（纯内存替身：FakeSession / FakePipeline / 记录型 resume_starter）：

- 四决策状态映射与留痕（decided_by/decided_at/decision/scope）；
- 批次顺序（created_at 递增微秒）与 resume 决策的顺序（按行序）；
- 重复决策 409 / 未知审批 404 / edit 缺参、respond 缺反馈 422；
- `scope=session` 把会话 approval_mode 升到 auto（PRD L307）；
- AUTO 留痕接口（Phase 3 占位）：status=auto_approved / decided_by=classifier / decision=None；
- stop 清理：pending → rejected(decided_by=system) + 广播；
- resume 守卫：run 非 waiting_approval 态不触发续跑。

DB 语义（真实约束/事件落库）由 integration `test_approval_flow.py` 覆盖——本文件只锁
service 的分支与顺序，不碰真实 SQL。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from loguru import logger

from app.exception.approval import ApprovalAlreadyResolved, ApprovalNotFound
from app.exception.common import InvalidArgument
from app.models import Approval, Session
from app.models import Run as RunRow
from app.services.approval_service import (
    ApprovalService,
    PendingApproval,
    ResumeDecision,
    create_pending_batch_short_tx,
)
from lcode_contracts.rest import ApprovalDecisionRequest

SESSION_ID = uuid.UUID("3f1c9a52-6f77-4a0f-9a1e-7b2c4d5e6f70")
RUN_ID = uuid.UUID("9d8e7f60-1a2b-4c3d-8e4f-5a6b7c8d9e01")


# ---------------------------------------------------------------- 替身


class _ScalarResult:
    """`await session.execute(...)` 的最小结果替身（只撑起 `scalar_one_or_none`）。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """AsyncSession 最小替身：只实现本服务用到的面（add/flush/commit/get/delete）。"""

    def __init__(
        self,
        rows: dict[tuple[type, uuid.UUID], Any] | None = None,
        *,
        execute_result: Any = None,
        scalar_result: Any = None,
    ) -> None:
        self.rows = rows or {}
        self.added: list[Any] = []
        self.ops: list[str] = []
        self.execute_result = execute_result
        self.scalar_result = scalar_result
        self.statements: list[Any] = []

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.rows[(type(obj), obj.id)] = obj
        self.added.append(obj)

    async def flush(self) -> None:
        self.ops.append("flush")

    async def commit(self) -> None:
        self.ops.append("commit")

    async def rollback(self) -> None:
        self.ops.append("rollback")

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self.execute_result if self.execute_result is not None else _ScalarResult(None)

    async def scalar(self, statement: Any) -> Any:
        """`session.scalar(...)`：AUTO 留痕的幂等判重（默认无既有行）。"""
        self.statements.append(statement)
        return self.scalar_result

    async def get(self, model: type, key: uuid.UUID, **kwargs: Any) -> Any:
        # `with_for_update` / `populate_existing` 在内存替身里无并发/无库可锁，忽略即可；
        # 行级锁与漏唤醒的并发语义由 integration test_approval_flow 的真 PG 用例覆盖。
        del kwargs
        return self.rows.get((model, key))

    async def scalars(self, statement: Any) -> Any:  # pragma: no cover —— 本文件不走 SQL
        raise AssertionError("单测不应执行真实查询（请注入行集合替身）")


class FakeSessionFactory:
    """async_sessionmaker 替身：`async with factory() as session` 返回同一个 FakeSession。"""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSessionFactory:
        return self

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePipeline:
    """EventPipeline 替身：记录事件并把 commit 顺序一并记下（验证「先落库后广播」）。"""

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.events: list[Any] = []

    async def emit(self, event: Any) -> Any:
        self.session.ops.append("emit")
        self.events.append(event)
        return event


class ResumeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ResumeDecision]]] = []

    def __call__(self, run_id: str, decisions: list[ResumeDecision]) -> None:
        self.calls.append((run_id, decisions))


class BoomPipeline:
    """事件出口故障替身：`emit` 必抛（验证「回声失败不阻断续跑」）。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def emit(self, event: Any) -> Any:
        self.attempts += 1
        raise RuntimeError("event bus down")


class FlakyPipeline(FakePipeline):
    """按次抛错的管道替身：第 `fail_at` 次（可多个）emit 失败，其余照常记录。"""

    def __init__(self, session: FakeSession, *fail_at: int) -> None:
        super().__init__(session)
        self._fail_at = frozenset(fail_at)
        self.attempts = 0

    async def emit(self, event: Any) -> Any:
        self.attempts += 1
        if self.attempts in self._fail_at:
            raise RuntimeError("event bus down")
        return await super().emit(event)


def _approval(
    *,
    approval_id: uuid.UUID | None = None,
    tool: str = "execute",
    tool_call_id: str = "call-1",
    status: str = "pending",
    created_at: datetime | None = None,
    run_id: uuid.UUID | None = RUN_ID,
    decision: str | None = None,
) -> Approval:
    row = Approval(
        id=approval_id or uuid.uuid4(),
        user_id="local",
        session_id=SESSION_ID,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool=tool,
        args={"command": "echo hi"},
        risk_level="high",
        reason="沙箱命令执行",
        status=status,
        decision=decision,
        scope="once",
        created_at=created_at or datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )
    return row


def _run_row(status: str = "waiting_approval") -> RunRow:
    return RunRow(
        id=RUN_ID,
        user_id="local",
        session_id=SESSION_ID,
        status=status,
        started_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )


def _session_row(*, approval_mode: str = "manual") -> Session:
    """会话行替身（`scope=session` 升级 / 反例不变量的断言对象）。"""
    return Session(
        id=SESSION_ID,
        user_id="local",
        title="t",
        status="active",
        model="m",
        reasoning_effort="high",
        approval_mode=approval_mode,
        sandbox_provider="bailian",
        updated_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        last_active_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )


def _service(
    rows: list[Approval],
    *,
    run_status: str = "waiting_approval",
    session_row: Session | None = None,
    factory: FakeSessionFactory | None = None,
) -> tuple[ApprovalService, FakeSession, FakePipeline, ResumeRecorder]:
    session = factory.session if factory is not None else FakeSession()
    # 短事务出口（scope=session 的会话升级）也必须走替身：单测零真实库访问
    factory = factory if factory is not None else FakeSessionFactory(session)
    session.rows[(RunRow, RUN_ID)] = _run_row(run_status)
    if session_row is not None:
        session.rows[(Session, SESSION_ID)] = session_row
    for row in rows:  # 审批行按主键可 get（decide/close 的入口查询）
        session.rows[(Approval, row.id)] = row
    pipeline = FakePipeline(session)
    recorder = ResumeRecorder()
    service = ApprovalService(
        session,  # type: ignore[arg-type]  # FakeSession 只撑起被调用的面
        pipeline=pipeline,  # type: ignore[arg-type]
        resume_starter=recorder,
        session_factory=factory,  # type: ignore[arg-type]
    )

    # 查询面用行集合替身（FakeSession 不执行 SQL）；排序口径与真实查询一致
    async def pending_for_run(run_id: uuid.UUID, **_kwargs: Any) -> list[Approval]:
        return [row for row in rows if row.run_id == run_id and row.status == "pending"]

    async def rows_for_run(run_id: uuid.UUID) -> list[Approval]:
        selected = [row for row in rows if row.run_id == run_id]
        return sorted(selected, key=lambda row: (row.created_at, str(row.id)))

    monkeypatch_attrs = {"_pending_for_run": pending_for_run, "_rows_for_run": rows_for_run}
    for name, value in monkeypatch_attrs.items():
        setattr(service, name, value)
    return service, session, pipeline, recorder


# ---------------------------------------------------------------- pending 批


async def test_create_pending_batch_increments_created_at_by_microsecond() -> None:
    """一事务写 N 行 pending；created_at 按入参序递增微秒（D3 的稳定顺序）。"""
    session = FakeSession()
    factory = FakeSessionFactory(session)

    items = await create_pending_batch_short_tx(
        session_id=str(SESSION_ID),
        run_id=str(RUN_ID),
        user_id="local",
        items=[
            PendingApproval("call-1", "execute", {"command": "a"}, "high", "r1"),
            PendingApproval("call-2", "write_file", {"file_path": "/a"}, "medium", "r2"),
        ],
        session_factory=factory,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert [item.status for item in items] == ["pending", "pending"]
    assert [item.risk_level for item in items] == ["high", "medium"]
    rows = [obj for obj in session.added if isinstance(obj, Approval)]
    assert len(rows) == 2
    assert rows[1].created_at - rows[0].created_at == timedelta(microseconds=1)
    assert session.ops[-1] == "commit", "批必须 commit 后才返回（事件只引用已提交事实）"


# ---------------------------------------------------------------- 四决策


async def test_approve_marks_row_and_emits_after_commit() -> None:
    """approve：行落 approved（decided_by=user），事件在 commit 之后发出。"""
    row = _approval()
    service, session, pipeline, recorder = _service([row])

    item, run_id = await service.decide(str(row.id), ApprovalDecisionRequest(decision="approve"))

    assert item.status == "approved"
    assert item.decision == "approve"
    assert row.decided_by == "user"
    assert row.decided_at is not None
    assert run_id == str(RUN_ID)
    assert session.ops.index("commit") < session.ops.index("emit")
    assert [event.decision for event in pipeline.events] == ["approve"]
    assert pipeline.events[0].decided_by == "user"
    assert recorder.calls == [  # 唯一的 pending 已解决 → resume（顺序按行）
        (str(RUN_ID), [ResumeDecision(tool_call_id="call-1", decision="approve", tool="execute")])
    ]


async def test_decide_emit_failure_still_starts_resume() -> None:
    """回声 emit 抛异常：决策已提交，必须记 ERROR 且**照旧触发 resume**（code review P2）。

    顺序保持「先回声后 resume」；但 echo 故障若冒泡，run 会永久停在 waiting_approval
    （客户端无回声、也没有 resume）。
    """
    row = _approval()
    service, _session, _pipeline, recorder = _service([row])
    boom = BoomPipeline()
    setattr(service, "_pipeline", boom)  # noqa: B010 —— 测试替身只在实例上换出口
    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        item, run_id = await service.decide(
            str(row.id), ApprovalDecisionRequest(decision="approve")
        )
    finally:
        logger.remove(sink_id)

    assert (item.status, item.decision) == ("approved", "approve")
    assert run_id == str(RUN_ID)
    assert boom.attempts == 1, "回声仍要先尝试发送（顺序不能反）"
    assert len(recorder.calls) == 1, "回声故障不得阻断续跑"
    errors = [
        record
        for record in captured
        if record["level"].name == "ERROR" and record["extra"].get("entity") == "approval"
    ]
    assert errors, "回声发射失败必须留 ERROR 日志"


async def test_upgrade_session_scope_failure_does_not_block_decision() -> None:
    """`scope=session` 升级失败不得改写决策成败（commit 后副作用同族隔离）。

    决策已落库就是成功：副作用失败只留可审计 ERROR，回声与 resume 照旧
    （否则客户端 500、回声/续跑都不发，run 永久停在 waiting_approval）。
    """
    session_row = _session_row()
    row = _approval()
    service, _session, pipeline, recorder = _service([row], session_row=session_row)

    async def _boom(_session_id: uuid.UUID) -> None:
        raise RuntimeError("session upgrade failed")

    setattr(service, "_upgrade_session_scope", _boom)  # noqa: B010 —— 测试替身

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        item, run_id = await service.decide(
            str(row.id), ApprovalDecisionRequest(decision="approve", scope="session")
        )
    finally:
        logger.remove(sink_id)

    assert (item.status, item.scope) == ("approved", "session")
    assert (row.status, row.decided_by) == ("approved", "user")
    assert run_id == str(RUN_ID)
    assert [event.decision for event in pipeline.events] == ["approve"], "升级失败不得吞掉决策回声"
    assert len(recorder.calls) == 1, "升级失败不得阻断续跑"
    assert session_row.approval_mode == "manual", "升级失败时会话审批模式保持原值"
    errors = [
        record
        for record in captured
        if record["level"].name == "ERROR" and record["extra"].get("entity") == "session"
    ]
    assert errors, "副作用失败必须留可审计的 ERROR 日志"


async def test_resume_waits_for_all_pending_and_orders_decisions() -> None:
    """全部 pending 解决前不 resume；最后一条决策触发且决策列表按 created_at 排序。"""
    first = _approval(tool_call_id="call-1")
    second = _approval(
        tool_call_id="call-2",
        tool="write_file",
        created_at=first.created_at + timedelta(microseconds=1),
    )
    service, _session, _pipeline, recorder = _service([first, second])

    await service.decide(str(first.id), ApprovalDecisionRequest(decision="approve"))
    assert recorder.calls == []  # 还有 pending，不 resume

    await service.decide(
        str(second.id),
        ApprovalDecisionRequest(decision="edit", edited_args={"file_path": "/b"}),
    )
    assert len(recorder.calls) == 1
    _run_id, decisions = recorder.calls[0]
    assert [d.tool_call_id for d in decisions] == ["call-1", "call-2"]
    assert [d.decision for d in decisions] == ["approve", "edit"]
    assert decisions[1].to_hitl_decision() == {
        "type": "edit",
        "edited_action": {"name": "write_file", "args": {"file_path": "/b"}},
    }


async def test_reject_and_respond_carry_feedback_into_hitl_payload() -> None:
    reject_row = _approval(tool_call_id="call-1")
    respond_row = _approval(
        tool_call_id="call-2", created_at=reject_row.created_at + timedelta(microseconds=1)
    )
    service, _session, _pipeline, recorder = _service([reject_row, respond_row])

    await service.decide(
        str(reject_row.id), ApprovalDecisionRequest(decision="reject", feedback="不要执行")
    )
    await service.decide(
        str(respond_row.id), ApprovalDecisionRequest(decision="respond", feedback="人工代答")
    )

    decisions = recorder.calls[0][1]
    assert decisions[0].to_hitl_decision() == {"type": "reject", "message": "不要执行"}
    assert decisions[1].to_hitl_decision() == {"type": "respond", "message": "人工代答"}


async def test_repeated_decision_is_409() -> None:
    row = _approval(status="approved", decision="approve")
    service, *_ = _service([row])

    with pytest.raises(ApprovalAlreadyResolved):
        await service.decide(str(row.id), ApprovalDecisionRequest(decision="reject"))


async def test_unknown_approval_is_404() -> None:
    service, *_ = _service([])
    with pytest.raises(ApprovalNotFound):
        await service.decide(str(uuid.uuid4()), ApprovalDecisionRequest(decision="approve"))


async def test_invalid_decision_payload_is_422() -> None:
    """service 层二道守门（契约 DTO 之外）：edit 缺参 / respond 空反馈。"""
    edit_row = _approval()
    respond_row = _approval(tool_call_id="call-2", created_at=edit_row.created_at)
    service, *_ = _service([edit_row, respond_row])

    bad_edit = ApprovalDecisionRequest.model_construct(decision="edit", edited_args=None)
    with pytest.raises(InvalidArgument):
        await service.decide(str(edit_row.id), bad_edit)

    bad_respond = ApprovalDecisionRequest.model_construct(decision="respond", feedback="  ")
    with pytest.raises(InvalidArgument):
        await service.decide(str(respond_row.id), bad_respond)


async def test_edit_with_empty_edited_args_is_rejected() -> None:
    """`edited_args={}` 等同 approve 却留「用户编辑过」痕迹 → 422（code review P3）。"""
    row = _approval()
    service, *_ = _service([row])

    with pytest.raises(InvalidArgument):
        await service.decide(str(row.id), ApprovalDecisionRequest(decision="edit", edited_args={}))

    assert row.status == "pending", "非法决策不得落行"
    assert row.decision is None


async def test_scope_session_upgrades_session_approval_mode() -> None:
    """scope=session（P0 的「持续批准」）= 会话 approval_mode 升到 auto（PRD L307）。"""
    session_row = _session_row()
    row = _approval()
    service, _session, _pipeline, _recorder = _service([row], session_row=session_row)

    item, _run_id = await service.decide(
        str(row.id), ApprovalDecisionRequest(decision="approve", scope="session")
    )

    assert item.scope == "session"
    assert session_row.approval_mode == "auto"


async def test_session_scope_is_rejected_for_reject_and_respond() -> None:
    """`scope=session` 只允许 approve/edit：reject/respond 带 session → 422 且不升级。

    安全审计 P2-2：否则「拒绝并始终拒绝」会被实现成「拒绝并放开自动审批」。
    """
    session_row = _session_row()
    reject_row = _approval(tool_call_id="call-1")
    respond_row = _approval(
        tool_call_id="call-2", created_at=reject_row.created_at + timedelta(microseconds=1)
    )
    service, _session, _pipeline, recorder = _service(
        [reject_row, respond_row], session_row=session_row
    )

    with pytest.raises(InvalidArgument):
        await service.decide(
            str(reject_row.id), ApprovalDecisionRequest(decision="reject", scope="session")
        )
    with pytest.raises(InvalidArgument):
        await service.decide(
            str(respond_row.id),
            ApprovalDecisionRequest(decision="respond", scope="session", feedback="人工代答"),
        )

    assert session_row.approval_mode == "manual", "非法组合不得升级会话审批模式"
    assert (reject_row.status, respond_row.status) == ("pending", "pending")
    assert (reject_row.decision, respond_row.decision) == (None, None)
    assert recorder.calls == []


async def test_resume_not_triggered_when_run_left_waiting() -> None:
    """run 已不在 waiting_approval（并发 stop/error）：决策照落行，但不 resume。"""
    row = _approval()
    service, _session, _pipeline, recorder = _service([row], run_status="stopped")

    await service.decide(str(row.id), ApprovalDecisionRequest(decision="approve"))

    assert recorder.calls == []


# ---------------------------------------------------------------- AUTO 留痕 / stop 清理


async def test_record_auto_approval_writes_classifier_row() -> None:
    """AUTO 留痕（Phase 3 接口）：status=auto_approved / decided_by=classifier / 无决策字面量。"""
    session = FakeSession()
    factory = FakeSessionFactory(session)
    service = ApprovalService(
        session,  # type: ignore[arg-type]
        pipeline=FakePipeline(session),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
    )

    item = await service.record_auto_approval(
        session_id=str(SESSION_ID),
        run_id=str(RUN_ID),
        user_id="local",
        item=PendingApproval("call-task", "task", {"description": "x"}, "medium", "规则放行"),
    )

    assert item.status == "auto_approved"
    assert item.decision is None
    row = session.added[0]
    assert row.decided_by == "classifier"
    assert row.reason == "规则放行"


async def test_record_auto_approval_is_idempotent_by_tool_call_id() -> None:
    """审计幂等：同 (session, run, tool_call_id) 已有 auto_approved 行 → 不再插新行。"""
    existing = _approval(tool="task", tool_call_id="call-task", status="auto_approved")
    existing.decided_by = "classifier"
    existing.reason = "规则放行"
    session = FakeSession(scalar_result=existing)
    service = ApprovalService(
        session,  # type: ignore[arg-type]
        pipeline=FakePipeline(session),  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(session),  # type: ignore[arg-type]
    )

    item = await service.record_auto_approval(
        session_id=str(SESSION_ID),
        run_id=str(RUN_ID),
        user_id="local",
        item=PendingApproval("call-task", "task", {"description": "x"}, "medium", "规则放行"),
    )

    assert item.id == str(existing.id)
    assert session.added == [], "既有审计行存在时不得重复落行"


async def test_close_pending_for_run_rejects_and_broadcasts() -> None:
    """stop 清理：pending → rejected(decided_by=system) 并逐条广播 approval_resolved。"""
    first = _approval(tool_call_id="call-1")
    second = _approval(
        tool_call_id="call-2", created_at=first.created_at + timedelta(microseconds=1)
    )
    service, subject_session, pipeline, _recorder = _service([first, second])

    closed = await service.close_pending_for_run(str(RUN_ID), feedback="运行已停止，审批自动驳回")

    assert len(closed) == 2
    assert {row.status for row in (first, second)} == {"rejected"}
    assert {row.decided_by for row in (first, second)} == {"system"}
    assert {row.feedback for row in (first, second)} == {"运行已停止，审批自动驳回"}
    assert subject_session.ops.index("commit") < subject_session.ops.index("emit")
    assert [event.decision for event in pipeline.events] == ["reject", "reject"]
    assert {event.decided_by for event in pipeline.events} == {"system"}


async def test_close_pending_for_run_is_idempotent() -> None:
    """没有 pending 行时返回空列表（重复 stop 幂等）。"""
    service, *_ = _service([_approval(status="rejected", decision="reject")])
    assert await service.close_pending_for_run(str(RUN_ID), feedback="x") == []


async def test_close_pending_skips_rows_resolved_by_concurrent_decide() -> None:
    """并发守卫：读到的行若已被用户决策解决，stop 必须跳过且不回声（安全审计 P2-1）。

    真实库语义（FOR UPDATE + EvalPlanQual 过滤）由 integration 的 decide×stop 用例覆盖；
    本用例锁定第二道行级守卫：已决行绝不能被 system 覆盖成 rejected，也不能发矛盾回声。
    """
    user_row = _approval(status="approved", decision="approve", tool_call_id="call-1")
    still_pending = _approval(
        tool_call_id="call-2",
        created_at=user_row.created_at + timedelta(microseconds=1),
    )
    service, _session, pipeline, _recorder = _service([user_row, still_pending])

    async def stale_pending(run_id: uuid.UUID, **_kwargs: Any) -> list[Approval]:
        assert run_id == RUN_ID
        return [user_row, still_pending]  # 模拟「读到的行已被 decide 抢先解决」

    setattr(service, "_pending_for_run", stale_pending)  # noqa: B010 —— 测试替身
    closed = await service.close_pending_for_run(str(RUN_ID), feedback="运行已停止")

    assert closed == [str(still_pending.id)]
    assert (user_row.status, user_row.decision) == ("approved", "approve"), (
        "已决的用户行不得被 stop 覆盖"
    )
    assert user_row.feedback is None
    assert (still_pending.status, still_pending.decided_by) == ("rejected", "system")
    assert [event.decision for event in pipeline.events] == ["reject"]
    assert [event.decided_by for event in pipeline.events] == ["system"]


def _batch(count: int) -> list[Approval]:
    """同批 N 条 pending（created_at 依次 +1μs，保证稳定顺序）。"""
    first = _approval(tool_call_id="call-1")
    rows = [first]
    for offset in range(1, count):
        rows.append(
            _approval(
                tool_call_id=f"call-{offset + 1}",
                created_at=first.created_at + timedelta(microseconds=offset),
            )
        )
    return rows


async def test_close_pending_emits_remaining_rows_after_single_emit_failure() -> None:
    """逐条隔离：第 2 条回声失败，第 1/3 条照常发出（一条 ERROR，循环不中断）。"""
    rows = _batch(3)
    service, subject_session, _pipeline, _recorder = _service(rows)
    flaky = FlakyPipeline(subject_session, 2)
    setattr(service, "_pipeline", flaky)  # noqa: B010 —— 测试替身

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        closed = await service.close_pending_for_run(str(RUN_ID), feedback="运行已停止")
    finally:
        logger.remove(sink_id)

    assert closed == [str(row.id) for row in rows]
    assert {row.status for row in rows} == {"rejected"}, "回声故障不得回滚已提交的驳回"
    assert flaky.attempts == 3, "三条都必须尝试发射（失败不中断循环）"
    assert [event.approval_id for event in flaky.events] == [str(rows[0].id), str(rows[2].id)]
    errors = [
        record
        for record in captured
        if record["level"].name == "ERROR" and record["extra"].get("entity") == "approval"
    ]
    assert len(errors) == 1, "单条失败只对应一条 ERROR"


async def test_close_pending_survives_multiple_emit_failures() -> None:
    """多条回声失败：每条各自记 ERROR，成功的那条照常发出（不中断循环）。"""
    rows = _batch(4)
    service, subject_session, _pipeline, _recorder = _service(rows)
    flaky = FlakyPipeline(subject_session, 2, 4)
    setattr(service, "_pipeline", flaky)  # noqa: B010 —— 测试替身

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        closed = await service.close_pending_for_run(str(RUN_ID), feedback="运行已停止")
    finally:
        logger.remove(sink_id)

    assert closed == [str(row.id) for row in rows]
    assert flaky.attempts == 4
    assert [event.approval_id for event in flaky.events] == [str(rows[0].id), str(rows[2].id)]
    errors = [
        record
        for record in captured
        if record["level"].name == "ERROR" and record["extra"].get("entity") == "approval"
    ]
    assert len(errors) == 2, "两条失败回声各自留一条 ERROR"


# ---------------------------------------------------------------- resume 启动失败兜底


def _bare_service(session: FakeSession, pipeline: Any) -> ApprovalService:
    """只装配兜底小事务所需的面（不注入 resume_starter）。"""
    return ApprovalService(
        session,  # type: ignore[arg-type]
        pipeline=pipeline,
        session_factory=FakeSessionFactory(session),  # type: ignore[arg-type]
    )


async def test_fail_run_after_resume_error_keeps_prior_terminal_state() -> None:
    """条件迁移：run 已被 stop/error 抢先收口 → 不覆盖终态、不发第二个终态事件。"""
    session = FakeSession(execute_result=_ScalarResult(None))
    pipeline = FakePipeline(session)
    service = _bare_service(session, pipeline)

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        await service._fail_run_after_resume_error(str(RUN_ID))
    finally:
        logger.remove(sink_id)

    assert "rollback" in session.ops and "commit" not in session.ops
    assert pipeline.events == [], "终态归抢先方，不得再发 run_status(error)"
    warnings = [
        record
        for record in captured
        if record["level"].name == "WARNING" and record["extra"].get("entity") == "run"
    ]
    assert warnings, "未迁移必须留 WARNING（否则无从解释 run 为何没收敛）"


async def test_fail_run_after_resume_error_is_best_effort() -> None:
    """兜底自身失败（回声发射抛错）只记 ERROR，不向调用方抛出（决策 ack 已成功）。"""
    session = FakeSession(execute_result=_ScalarResult(SESSION_ID))
    service = _bare_service(session, BoomPipeline())

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        await service._fail_run_after_resume_error(str(RUN_ID))  # 不抛出
    finally:
        logger.remove(sink_id)

    assert "commit" in session.ops, "行收敛已提交（终态已落库），事件失败不回滚行"
    errors = [
        record
        for record in captured
        if record["level"].name == "ERROR" and record["extra"].get("entity") == "run"
    ]
    assert errors, "兜底失败必须留 ERROR"
