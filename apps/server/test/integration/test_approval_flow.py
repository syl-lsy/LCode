"""审批主链 integration（M3 Phase 1，真 PG；REQ-006 / AC-006-1~5 / AC-013）。

覆盖（对应 m3-plan §4 Phase 1 的验收面）：

1. **挂起**：execute 门控 → interrupt → approvals pending 行 + `approval_request` 事件 +
   run 行 `waiting_approval`（无 finished_at）+ 不发 `run_status(finished)` + 工具未执行；
2. **四决策**：approve（工具执行）/ reject（error ToolMessage、不执行）/ edit（执行改写
   参数且 when 不翻转）/ respond（人工代答、不执行）；
3. **刷新恢复**：`GET /approvals?status=pending&session_id=` 返回挂起卡片（AC-006-5）；
4. **多订阅者**：决策后每个订阅者都收到 `approval_resolved`（多标签页一致，AC-008-3）；
5. **重复决策 409**；未知审批 404；
6. **跨段 usage_totals**：最终段 `run_status(finished).usage_totals` = 前一段 + 本段；
   model_calls 无重复记账（resume 播种生效）。

替身边界：模型=ScriptedChatModel、沙箱=FakeSandbox（真 tmp + 真子进程）、
checkpointer=InMemorySaver（REQ-004 约束④；PG saver 的跨实例能力由 m3-spikes §2 取证）；
事件管线/DB 走真件。
"""

import asyncio
import contextlib
import time
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import build_database_url, get_session_factory
from app.exception.approval import ApprovalAlreadyResolved
from app.models import Approval, Session
from app.models import Run as RunRow
from app.services.approval_service import ApprovalService, ResumeDecision
from app.services.run_service import RunService
from lcode_contracts.common import Usage
from lcode_contracts.rest import (
    ApprovalDecisionAck,
    ApprovalDecisionRequest,
    ApprovalItem,
    ApprovalPage,
)
from test.support import scripts
from test.support.approval_env import (
    APPROVALS,
    approvals_of,
    create_session,
    event_payload,
    events_of,
    latest_run,
    model_calls_of,
    post_message,
    unwrap,
    wait_for_approvals,
    wait_for_events,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel

pytestmark = pytest.mark.requires_db


async def _park_execute(client, env, db, content: str = "跑一条命令"):
    """发起一次会被审批挂起的 execute run，返回 (session_id, approval_row, run_row)。"""
    created = await create_session(client, title="审批主链")
    env.set_model(ScriptedChatModel(steps=scripts.approval_execute()))

    response = await post_message(client, created.id, content)
    assert response.status_code == 200, response.text

    await wait_run_state(client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db, created.id, count=1)
    # 停车完成的判据是**事件已落库**（run 行/审批行都在事件之前提交，单看它们会
    # 拿到「停车尚未走完」的中间态——事件之后的 model_calls 可能还在写）。
    await wait_for_events(db, created.id, "approval_request")
    run = await latest_run(db, created.id)
    return created.id, rows[0], run


async def _decide(client, approval_id: uuid.UUID, **payload: object) -> ApprovalDecisionAck:
    response = await client.post(f"{APPROVALS}/{approval_id}/decide", json=payload)
    assert response.status_code == 200, response.text
    return ApprovalDecisionAck.model_validate(unwrap(response))


# ---------------------------------------------------------------- 挂起态


async def test_execute_interrupt_creates_pending_approval_and_waiting_run(
    async_client, approval_env, db_session
) -> None:
    """挂起：pending 审批行 + approval_request 事件 + run waiting_approval + 工具未执行。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)

    # 审批行（AC-006-1/5：完整入参、风险等级、理由、挂起态）
    assert approval.status == "pending"
    assert approval.tool == "execute"
    assert approval.risk_level == "high"
    assert approval.args == {"command": scripts.APPROVAL_COMMAND}
    assert approval.tool_call_id == scripts.APPROVAL_TOOL_CALL_ID
    assert approval.reason and "沙箱红线" in approval.reason
    assert approval.run_id == run.id

    # run 行：挂起不是终态（D4）
    assert run.status == "waiting_approval"
    assert run.finished_at is None

    # 事件：approval_request 已落库；同一 run 没有 finished/error 收口
    events = await events_of(db_session, session_id)
    requests = event_payload(events, "approval_request")
    assert len(requests) == 1
    assert requests[0]["approval_id"] == str(approval.id)
    assert requests[0]["tool_call_id"] == scripts.APPROVAL_TOOL_CALL_ID
    assert requests[0]["risk_level"] == "high"
    finished = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") in ("finished", "error", "stopped")
    ]
    assert finished == []

    # 工具没有执行（审批是唯一闸门）
    assert approval_env.sandboxes[session_id].tool_commands == []

    # 刷新恢复入口（AC-006-5）
    page = ApprovalPage.model_validate(
        unwrap(await async_client.get(f"{APPROVALS}?status=pending&session_id={session_id}"))
    )
    assert [item.id for item in page.items] == [str(approval.id)]
    detail = ApprovalItem.model_validate(
        unwrap(await async_client.get(f"{APPROVALS}/{approval.id}"))
    )
    assert detail.status == "pending"


# ---------------------------------------------------------------- 四决策


async def test_approve_executes_tool_and_finishes_with_cross_segment_usage(
    async_client, approval_env, db_session
) -> None:
    """approve：工具执行 → run completed；usage_totals 跨段合计、model_calls 不重复。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)
    sandbox = approval_env.sandboxes[session_id]

    ack = await _decide(async_client, approval.id, decision="approve")
    assert ack.approval.status == "approved"
    assert ack.approval.decision == "approve"
    assert ack.run_id == str(run.id)

    await wait_run_state(async_client, session_id, "idle")
    assert sandbox.tool_commands == [scripts.APPROVAL_COMMAND]

    row = await db_session.get(RunRow, run.id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status == "completed"

    # 跨段 usage：首段 10/5 + 最终段 20/8 = 30/13（只算最后一段会少计，D4）
    events = await events_of(db_session, session_id)
    finished = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") == "finished"
    ]
    assert len(finished) == 1
    assert Usage.model_validate(finished[0]["usage_totals"]) == Usage(
        input_tokens=30, output_tokens=13
    )

    # 逐次记账：两段各一次模型调用，resume 段不重复记挂起回合（10/5 只出现一次）
    calls = await model_calls_of(db_session, run.id)
    assert len(calls) == 2
    assert sum(call.input_tokens for call in calls) == 30
    assert sum(call.output_tokens for call in calls) == 13

    # approval_resolved 已落库（决策回声，决策人=user）
    resolved = event_payload(events, "approval_resolved")
    assert len(resolved) == 1
    assert resolved[0]["decision"] == "approve"
    assert resolved[0]["decided_by"] == "user"


async def test_approve_records_decision_columns(async_client, approval_env, db_session) -> None:
    """AC-006-2：决策人/决策时刻/决策全文留痕。"""
    _session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)

    await _decide(async_client, approval.id, decision="approve", scope="once")
    await wait_run_state(async_client, _session_id, "idle")

    row = await db_session.get(Approval, approval.id)
    assert row is not None
    await db_session.refresh(row)
    assert (row.status, row.decision, row.scope, row.decided_by) == (
        "approved",
        "approve",
        "once",
        "user",
    )
    assert row.decided_at is not None


async def test_reject_keeps_tool_unexecuted_and_reports_error(
    async_client, approval_env, db_session
) -> None:
    """reject：工具不执行，模型收到 error ToolMessage，run 正常收口。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)

    await _decide(async_client, approval.id, decision="reject", feedback="不要执行这条")
    await wait_run_state(async_client, session_id, "idle")

    assert approval_env.sandboxes[session_id].tool_commands == []
    row = await db_session.get(RunRow, run.id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status == "completed"
    events = await events_of(db_session, session_id)
    resolved = event_payload(events, "approval_resolved")
    assert resolved[0]["decision"] == "reject"
    assert resolved[0]["feedback"] == "不要执行这条"


async def test_edit_executes_edited_args_without_reinterrupting(
    async_client, approval_env, db_session
) -> None:
    """edit（AC-006-3）：执行改写后的参数；when 以原始参数评估，不二次中断。"""
    session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)

    await _decide(
        async_client,
        approval.id,
        decision="edit",
        edited_args={"command": "ls /tmp"},
    )
    await wait_run_state(async_client, session_id, "idle")

    # 改写后的命令执行；原命令不执行（when 不翻转：ls 白名单也不再触发 interrupt）
    assert approval_env.sandboxes[session_id].tool_commands == ["ls /tmp"]
    rows = await approvals_of(db_session, session_id)
    assert [(row.status, row.decision) for row in rows] == [("edited", "edit")]
    assert rows[0].edited_args == {"command": "ls /tmp"}


async def test_respond_synthesizes_tool_message_without_execution(
    async_client, approval_env, db_session
) -> None:
    """respond（AC-006-4）：人工代答作为成功 ToolMessage 回给模型；工具不执行。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)

    await _decide(async_client, approval.id, decision="respond", feedback="人工代答：跳过执行")
    await wait_run_state(async_client, session_id, "idle")

    assert approval_env.sandboxes[session_id].tool_commands == []
    row = await db_session.get(RunRow, run.id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status == "completed"
    rows = await approvals_of(db_session, session_id)
    assert rows[0].status == "responded"
    assert rows[0].feedback == "人工代答：跳过执行"


# ---------------------------------------------------------------- 多订阅者 / 409


async def test_multi_subscribers_receive_approval_resolved(
    async_client, approval_env, db_session
) -> None:
    """多标签页：决策后两个订阅者都收到 approval_resolved（AC-008-3）。"""
    session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)
    bus = approval_env.pipeline.bus
    first = bus.subscribe(session_id)
    second = bus.subscribe(session_id)

    await _decide(async_client, approval.id, decision="approve")

    async def _resolved(subscription) -> dict:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            event = await asyncio.wait_for(subscription.get(), timeout=5)
            if event.type == "approval_resolved":
                return event.model_dump()
        raise AssertionError("订阅者未收到 approval_resolved")

    payloads = await asyncio.gather(_resolved(first), _resolved(second))
    assert [payload["approval_id"] for payload in payloads] == [str(approval.id)] * 2
    await first.aclose()
    await second.aclose()
    await wait_run_state(async_client, session_id, "idle")


async def test_repeated_decision_is_409_and_unknown_is_404(
    async_client, approval_env, db_session
) -> None:
    """重复决策 → 409 APPROVAL_ALREADY_RESOLVED；未知审批 → 404 APPROVAL_NOT_FOUND。"""
    _session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)
    await _decide(async_client, approval.id, decision="approve")
    await wait_run_state(async_client, _session_id, "idle")

    response = await async_client.post(
        f"{APPROVALS}/{approval.id}/decide", json={"decision": "reject"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "APPROVAL_ALREADY_RESOLVED"

    missing = await async_client.get(f"{APPROVALS}/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "APPROVAL_NOT_FOUND"

    decision_missing = await async_client.post(
        f"{APPROVALS}/{uuid.uuid4()}/decide", json={"decision": "approve"}
    )
    assert decision_missing.status_code == 404
    assert decision_missing.json()["code"] == "APPROVAL_NOT_FOUND"


async def test_edit_without_args_is_422(async_client, approval_env, db_session) -> None:
    """契约 DTO 守门：edit 缺 edited_args → 422 INVALID_ARGUMENT（不落行）。"""
    _session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)

    response = await async_client.post(
        f"{APPROVALS}/{approval.id}/decide", json={"decision": "edit"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_ARGUMENT"
    row = await db_session.get(Approval, approval.id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status == "pending"


async def test_reject_with_session_scope_is_422_and_session_mode_unchanged(
    async_client, approval_env, db_session
) -> None:
    """`reject + scope=session` → 422 且会话审批模式保持原值（安全审计 P2-2）。

    语义选择：这是**语义级非法入参**（DTO 结构合法、业务含义非法），与同方法内
    edit 缺参/respond 空反馈同档 → 契约 `INVALID_ARGUMENT`(422)，不是状态冲突(409)。
    """
    session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)
    session_row = await db_session.get(Session, uuid.UUID(session_id))
    assert session_row is not None
    before = session_row.approval_mode

    response = await async_client.post(
        f"{APPROVALS}/{approval.id}/decide", json={"decision": "reject", "scope": "session"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_ARGUMENT"

    await db_session.refresh(session_row)
    assert session_row.approval_mode == before, "非法组合不得把会话审批模式升到 auto"
    row = await db_session.get(Approval, approval.id)
    assert row is not None
    await db_session.refresh(row)
    assert (row.status, row.decision, row.decided_by) == ("pending", None, None)


# ---------------------------------------------------------------- stop / 删除联动


async def test_stop_while_waiting_rejects_pending_approvals(
    async_client, approval_env, db_session
) -> None:
    """stop（D3）：挂起审批置 rejected(decided_by=system) 并广播；之后决策 → 409。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)

    response = await async_client.post(f"/api/v1/runs/{run.id}/stop")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "stopped"

    row = await db_session.get(Approval, approval.id)
    assert row is not None
    await db_session.refresh(row)
    assert (row.status, row.decision, row.decided_by) == ("rejected", "reject", "system")
    assert row.feedback and "停止" in row.feedback

    events = await events_of(db_session, session_id)
    resolved = event_payload(events, "approval_resolved")
    assert [(payload["decision"], payload["decided_by"]) for payload in resolved] == [
        ("reject", "system")
    ]

    late = await async_client.post(
        f"{APPROVALS}/{approval.id}/decide", json={"decision": "approve"}
    )
    assert late.status_code == 409, late.text


async def test_delete_session_with_pending_approval_purges_child_rows(
    async_client, approval_env, db_session
) -> None:
    """删除会话联动：approvals 先于 runs 删除（否则 FK 违约，m3-spikes §5 第 5 条）。"""
    session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)

    response = await async_client.delete(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 204, response.text

    remaining = await db_session.scalar(
        select(Approval).where(Approval.session_id == uuid.UUID(session_id))
    )
    assert remaining is None
    assert (
        await db_session.scalar(select(RunRow).where(RunRow.session_id == uuid.UUID(session_id)))
        is None
    )


# ---------------------------------------------------------------- 停车时序 / 并发收口（回归）


class _ResumeRecorder:
    """resume_starter 替身：只记录调度（真实续跑语义由其他用例覆盖）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ResumeDecision]]] = []

    def __call__(self, run_id: str, decisions: list[ResumeDecision]) -> None:
        self.calls.append((run_id, decisions))


async def _next_event(subscription: Any, event_type: str, *, timeout: float = 15.0) -> dict:
    """从会话订阅队列取指定类型事件（**不做任何 run_state / DB 轮询**）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, f"{timeout}s 内未收到 {event_type} 事件"
        try:
            event = await asyncio.wait_for(subscription.get(), timeout=remaining)
        except TimeoutError:
            raise AssertionError(f"{timeout}s 内未收到 {event_type} 事件") from None
        if event.type == event_type:
            return event.model_dump()


async def test_immediate_decide_after_approval_request_does_not_hang(
    async_client, approval_env, db_session, monkeypatch
) -> None:
    """生产时序回归：`approval_request` 一发布就决策必须续跑（不轮询 run_state）。

    钩子比真实客户端更极端：决策发生在 `emit()` 返回**之前**（真实客户端最早也只能
    在 emit 返回后拿到事件）。旧顺序（审批行 → emit → run 行挂起 → 播种）在这里必卡
    死：decide 的 `_start_resume` 守卫看到 run 仍是 running 而跳过续跑，审批行已
    approved，重试决策得 409 —— run 永久停在 waiting_approval。
    """
    created = await create_session(async_client, title="事件后立即决策")
    approval_env.set_model(ScriptedChatModel(steps=scripts.approval_execute()))
    subscription = approval_env.pipeline.bus.subscribe(created.id)
    original_emit = approval_env.pipeline.emit
    decided: list[str] = []
    decide_done = asyncio.Event()

    async def _emit_then_decide(event: Any) -> Any:
        stored = await original_emit(event)
        if stored.type == "approval_request" and not decided:
            decided.append(stored.approval_id)
            response = await async_client.post(
                f"{APPROVALS}/{decided[0]}/decide", json={"decision": "approve"}
            )
            assert response.status_code == 200, response.text
            decide_done.set()
        return stored

    monkeypatch.setattr(approval_env.pipeline, "emit", _emit_then_decide)
    try:
        response = await post_message(async_client, created.id, "跑一条命令")
        assert response.status_code == 200, response.text

        request = await _next_event(subscription, "approval_request")
        assert decided == [request["approval_id"]], "决策必须发生在 approval_request 发布之后"
        # 决策请求使用的测试会话与后续 REST 轮询共用——必须等它完整返回再发新请求
        # （否则同一 AsyncSession 会被两个请求并发使用）。
        await asyncio.wait_for(decide_done.wait(), timeout=10)

        resolved = await _next_event(subscription, "approval_resolved")
        assert resolved["approval_id"] == request["approval_id"]
        assert resolved["decision"] == "approve"

        await wait_run_state(async_client, created.id, "idle")
    finally:
        await subscription.aclose()

    # 续跑真的发生了：工具执行一次、run 收敛到 completed（不卡 waiting_approval）
    assert approval_env.sandboxes[created.id].tool_commands == [scripts.APPROVAL_COMMAND]
    run = await db_session.get(RunRow, uuid.UUID(request["run_id"]))
    assert run is not None
    await db_session.refresh(run)
    assert run.status == "completed"


async def test_run_is_already_waiting_when_approval_request_is_published(
    async_client, approval_env, monkeypatch
) -> None:
    """事件可见 ⇒ run 行**已**是 waiting_approval（客户端可立即决策的前置条件）。

    在 `emit()` 返回前直接读已提交行（不经 REST 投影）：旧顺序此时 run 还是 running
    （挂起行迁移在 emit 之后）——本用例在旧代码上确定性失败。
    """
    created = await create_session(async_client, title="停车事件时序")
    approval_env.set_model(ScriptedChatModel(steps=scripts.approval_execute()))
    subscription = approval_env.pipeline.bus.subscribe(created.id)
    original_emit = approval_env.pipeline.emit
    seen: list[tuple[str | None, datetime | None]] = []
    snapshot_done = asyncio.Event()

    async def _emit_then_snapshot(event: Any) -> Any:
        stored = await original_emit(event)
        if stored.type == "approval_request":
            async with get_session_factory()() as fresh:  # 新会话读已提交事实
                run = await fresh.get(RunRow, uuid.UUID(stored.run_id))
                seen.append(
                    (run.status if run is not None else None, run.finished_at if run else None)
                )
            snapshot_done.set()
        return stored

    monkeypatch.setattr(approval_env.pipeline, "emit", _emit_then_snapshot)
    try:
        assert (await post_message(async_client, created.id, "跑一条命令")).status_code == 200
        await _next_event(subscription, "approval_request")
        await asyncio.wait_for(snapshot_done.wait(), timeout=5)
        assert seen == [("waiting_approval", None)], (
            "approval_request 可见时 run 必须已离开 running 且未写 finished_at"
            f"（否则客户端立即决策会丢 resume），实际 {seen}"
        )
    finally:
        await subscription.aclose()


async def test_concurrent_decisions_in_same_batch_resume_exactly_once(
    async_client, approval_env, db_session
) -> None:
    """同批两条审批**并发**提交：两条决策都落行，只调度一次 resume。

    旧实现里两条决策可能各自看到对方仍是 pending 而都判定「未齐备」，谁都不 resume
    （漏唤醒）——run 行级锁把「落行 + 未决判断」串行化后，至少后到者必然看到全部已决。
    """
    created = await create_session(async_client, title="并发审批批次")
    approval_env.set_model(ScriptedChatModel(steps=scripts.approval_execute_batch()))
    response = await post_message(async_client, created.id, "并行跑两条命令")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=2)
    await wait_for_events(db_session, created.id, "approval_request", count=2)
    assert [row.tool_call_id for row in rows] == list(scripts.APPROVAL_BATCH_TOOL_CALL_IDS)

    recorder = _ResumeRecorder()
    factory = get_session_factory()

    async def _approve(approval_id: uuid.UUID) -> None:
        async with factory() as session:  # 每条决策独立会话 → 真并发事务
            service = ApprovalService(
                session, pipeline=approval_env.pipeline, resume_starter=recorder
            )
            await service.decide(str(approval_id), ApprovalDecisionRequest(decision="approve"))

    await asyncio.gather(*(_approve(row.id) for row in rows))

    assert len(recorder.calls) == 1, "同一批并发决策只能调度一次 resume（重复续跑会让图状态错位）"
    _run_id, decisions = recorder.calls[0]
    assert sorted(decision.tool_call_id for decision in decisions) == sorted(
        scripts.APPROVAL_BATCH_TOOL_CALL_IDS
    )
    async with factory() as fresh:  # 新会话读已提交事实（测试会话的 identity map 是旧快照）
        decided = list(
            await fresh.scalars(
                select(Approval)
                .where(Approval.session_id == uuid.UUID(created.id))
                .order_by(Approval.created_at.asc())
            )
        )
    assert [row.status for row in decided] == ["approved", "approved"]


async def test_concurrent_duplicate_decision_has_single_winner(
    async_client, approval_env, db_session
) -> None:
    """同一审批重复**并发**提交：一个 200、一个 409；只回声一条 approval_resolved。"""
    session_id, approval, _run = await _park_execute(async_client, approval_env, db_session)

    recorder = _ResumeRecorder()
    factory = get_session_factory()

    async def _approve() -> str:
        async with factory() as session:
            service = ApprovalService(
                session, pipeline=approval_env.pipeline, resume_starter=recorder
            )
            try:
                await service.decide(str(approval.id), ApprovalDecisionRequest(decision="approve"))
            except ApprovalAlreadyResolved:
                return "conflict"
            return "ok"

    outcomes = await asyncio.gather(_approve(), _approve())

    assert sorted(outcomes) == ["conflict", "ok"], "审批行级锁必须让后到者重读到已决状态"
    assert len(recorder.calls) == 1
    resolved = event_payload(await events_of(db_session, session_id), "approval_resolved")
    assert len(resolved) == 1, f"一次审批只允许一条决策回声：{resolved}"


# ---------------------------------------------------------------- decide × stop 竞态（安全审计 P2-1）


async def _wait_for_row_lock_wait(*, timeout_s: float = 10.0) -> None:
    """等某个会话进入行锁等待（`pg_stat_activity.wait_event_type = 'Lock'`）。

    本用例唯一的可能等待者就是 stop 的 pending 读（decide 被测试钩子停在内存事件上，
    不持有 DB 等待）。看到它 = 并发窗口已建立（stop 的 pending 读已发起且被行锁挡住），
    此时放行 decide 才能复现「读快照 → 覆盖」的旧缺陷。

    探针用**独立引擎**（NullPool）：不复用被测会话，避免探针自身的查询干扰
    并发窗口（测试经验：在共享测试会话上轮询会让 stop 的语句迟迟不到达服务端）。
    """
    deadline = time.monotonic() + timeout_s
    probe_engine = create_async_engine(build_database_url(), poolclass=NullPool)
    try:
        async with probe_engine.connect() as probe:
            while time.monotonic() < deadline:
                waiting = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                if waiting and int(waiting) > 0:
                    return
                await asyncio.sleep(0.02)
    finally:
        await probe_engine.dispose()
    pytest.fail("stop 未在超时前进入行锁等待（并发窗口未建立）")


async def test_concurrent_decide_and_stop_keeps_user_decision(
    async_client, approval_env, db_session, monkeypatch
) -> None:
    """decide 先提交、stop 并发进来：审批行保持 approved(user)，且只有一条回声。

    旧实现（普通 SELECT + 无条件覆盖）：stop 读到 pending（decide 未提交的快照），其
    UPDATE 阻塞在 decide 的行锁上；decide 提交后 stop 继续把 approved(user) 覆盖成
    rejected(system)——审计行与用户真实决策相反，并多发一条矛盾的 system 回声。
    """
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)
    factory = get_session_factory()
    decide_session = factory()
    stop_session = factory()
    recorder = _ResumeRecorder()
    decide_service = ApprovalService(
        decide_session, pipeline=approval_env.pipeline, resume_starter=recorder
    )

    locked = asyncio.Event()
    release = asyncio.Event()
    original_complete = ApprovalService._resume_decisions_if_complete

    async def _blocked(self: ApprovalService, decided: Approval) -> list[ResumeDecision] | None:
        # 此刻 decide 事务已 flush（审批行已 UPDATE）并持有行锁 —— 竞态窗口
        locked.set()
        await release.wait()
        return await original_complete(self, decided)

    monkeypatch.setattr(ApprovalService, "_resume_decisions_if_complete", _blocked)

    decide_task = asyncio.create_task(
        decide_service.decide(str(approval.id), ApprovalDecisionRequest(decision="approve"))
    )
    stop_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(locked.wait(), timeout=10)

        stop_service = RunService(
            stop_session, pipeline=approval_env.pipeline, manager=approval_env.manager
        )
        stop_task = asyncio.create_task(stop_service.stop_run(str(run.id)))
        await _wait_for_row_lock_wait()  # stop 已阻塞在 pending 读的行锁上

        release.set()
        ack = await asyncio.wait_for(stop_task, timeout=15)
        item, run_id = await asyncio.wait_for(decide_task, timeout=15)
    finally:
        release.set()
        for pending_task in (decide_task, stop_task):
            if pending_task is not None and not pending_task.done():
                pending_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending_task
        await decide_session.close()
        await stop_session.close()

    assert ack.status == "stopped"
    assert item.status == "approved"
    assert run_id == str(run.id)

    async with factory() as fresh:
        row = await fresh.get(Approval, approval.id)
        assert row is not None
        assert (row.status, row.decision, row.decided_by) == ("approved", "approve", "user"), (
            "用户已提交的决策不得被并发 stop 覆盖成 rejected(system)"
        )
        assert row.feedback is None, "stop 的驳回理由不得写进用户已决行"
        run_row = await fresh.get(RunRow, run.id)
        assert run_row is not None
        assert run_row.status == "stopped"
        assert run_row.finished_at is not None
        events = await events_of(fresh, session_id)

    resolved = event_payload(events, "approval_resolved")
    assert [(payload["decision"], payload["decided_by"]) for payload in resolved] == [
        ("approve", "user")
    ], "同一审批只允许一条决策回声（不得出现 approve/user + reject/system 的矛盾对）"
    terminal = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") in ("finished", "error", "stopped")
    ]
    assert [payload["phase"] for payload in terminal] == ["stopped"], "run 只能有一个终态"


async def test_stop_committed_first_makes_decide_409_without_user_echo(
    async_client, approval_env, db_session, monkeypatch
) -> None:
    """stop 先提交：decide 后到重读已决 → 409；审批行 rejected(system) 且只有 system 回声。"""
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)
    factory = get_session_factory()
    decide_session = factory()
    recorder = _ResumeRecorder()
    decide_service = ApprovalService(
        decide_session, pipeline=approval_env.pipeline, resume_starter=recorder
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    original_require = ApprovalService._require_pending_row

    async def _gated(self: ApprovalService, approval_id: str) -> Approval:
        entered.set()  # decide 在拿行锁之前停下：stop 可完整走完（提交先于用户决策）
        await release.wait()
        return await original_require(self, approval_id)

    monkeypatch.setattr(ApprovalService, "_require_pending_row", _gated)

    decide_task = asyncio.create_task(
        decide_service.decide(str(approval.id), ApprovalDecisionRequest(decision="approve"))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)

        stop_service = RunService(
            db_session, pipeline=approval_env.pipeline, manager=approval_env.manager
        )
        ack = await asyncio.wait_for(stop_service.stop_run(str(run.id)), timeout=15)
        assert ack.status == "stopped"

        release.set()
        with pytest.raises(ApprovalAlreadyResolved):
            await asyncio.wait_for(decide_task, timeout=15)
    finally:
        release.set()
        if not decide_task.done():
            decide_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await decide_task
        await decide_session.close()

    async with factory() as fresh:
        row = await fresh.get(Approval, approval.id)
        assert row is not None
        assert (row.status, row.decision, row.decided_by) == ("rejected", "reject", "system")
        run_row = await fresh.get(RunRow, run.id)
        assert run_row is not None
        assert run_row.status == "stopped"
        events = await events_of(fresh, session_id)

    resolved = event_payload(events, "approval_resolved")
    assert [(payload["decision"], payload["decided_by"]) for payload in resolved] == [
        ("reject", "system")
    ], "stop 已提交：用户决策必须 409，不得再发 approve(user) 回声"
    assert recorder.calls == []
    terminal = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") in ("finished", "error", "stopped")
    ]
    assert [payload["phase"] for payload in terminal] == ["stopped"], "run 只能有一个终态"


# ---------------------------------------------------------------- commit 后副作用隔离


async def test_resume_start_failure_converges_run_to_error(
    async_client, approval_env, db_session, monkeypatch
) -> None:
    """resume 启动失败：决策仍 200 落库，run 收敛为 error（而非永久 waiting_approval）。

    不收敛的后果（本用例的前置论证）：审批行已终态 → 重试 decide 409；P0 无 resume
    恢复端点 → 会话永久被活动 run 占住（新消息撞 RUN_ALREADY_ACTIVE）。error 是契约
    五值里唯一诚实且可恢复的终态：横幅带明确文案，重新发送消息即可开新一轮 run。
    """
    session_id, approval, run = await _park_execute(async_client, approval_env, db_session)

    def _boom(_self: ApprovalService, _run_id: str, _decisions: list[ResumeDecision]) -> None:
        raise RuntimeError("resume scheduler exploded")

    monkeypatch.setattr(ApprovalService, "_start_resume", _boom)

    ack = await _decide(async_client, approval.id, decision="approve")
    assert ack.approval.status == "approved"

    factory = get_session_factory()
    async with factory() as fresh:
        approval_row = await fresh.get(Approval, approval.id)
        assert approval_row is not None
        assert (approval_row.status, approval_row.decision, approval_row.decided_by) == (
            "approved",
            "approve",
            "user",
        ), "副作用失败不得改写已提交的决策"
        run_row = await fresh.get(RunRow, run.id)
        assert run_row is not None
        assert run_row.status == "error", "resume 启动失败必须收敛为 error"
        assert run_row.finished_at is not None
        assert run_row.error_code == "internal"
        assert run_row.error_message == "审批恢复启动失败，请重新发送消息重试"
        events = await events_of(fresh, session_id)

    resolved = event_payload(events, "approval_resolved")
    assert [(payload["decision"], payload["decided_by"]) for payload in resolved] == [
        ("approve", "user")
    ]
    terminal = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") in ("finished", "error", "stopped")
    ]
    assert [payload["phase"] for payload in terminal] == ["error"], "run 只能有一个终态"
    assert terminal[0]["error"]["code"] == "internal"
    assert terminal[0]["error"]["retryable"] is True
    assert "重新发送消息" in terminal[0]["error"]["message"]

    # 会话不再被活动 run 占住（run_state 由 runs 表推导，error 不参与）
    await wait_run_state(async_client, session_id, "idle")
