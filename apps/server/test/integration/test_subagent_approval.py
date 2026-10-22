"""子智能体审批 integration（M3 Phase 1，真 PG；REQ-016 / AC-013 / AC-016-2）。

链路（m3-spikes §1.2/§1.4 已证可行）：

1. MANUAL：主智能体 `task` 派 coder → **task 自身要审批**（默认表）→ 挂起；
2. 批准 task → 续跑：coder 在子图内 `execute` → 子图 interrupt 上浮到顶层 `()` →
   第二次挂起（父图 task 半开：started 已发、ToolMessage 未产）；
3. 批准 execute → coder 执行命令 → 子图收口 → 父图 task 返回 → 主智能体收尾。

锁定点：
- 声明式子智能体继承父 `interrupt_on`（spike §1.2：无需手挂 HITL）；
- 两次挂起的 approvals 行不互相串（批次按 run 内 created_at 排序）；
- 三次续跑后 usage_totals 仍为全 run 合计（跨段 + 子智能体调用都不丢/不重）。
"""

import uuid

import pytest

from lcode_contracts.common import Usage
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


async def test_subagent_execute_interrupts_and_resumes(
    async_client, approval_env, db_session
) -> None:
    """task 派 coder → 两次审批（task/execute）→ 命令执行一次 → 全链收口。"""
    created = await create_session(async_client, title="子智能体审批")
    approval_env.set_role_models(
        {
            "main": ScriptedChatModel(steps=scripts.subagent_task_then_reply()),
            "coder": ScriptedChatModel(steps=scripts.subagent_execute()),
        }
    )

    response = await post_message(async_client, created.id, "派 coder 跑一条命令")
    assert response.status_code == 200, response.text

    # 第一挂起：task 派发（MANUAL 默认表）
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=1)
    await wait_for_events(db_session, created.id, "approval_request")
    task_approval = rows[0]
    assert (task_approval.tool, task_approval.status) == ("task", "pending")
    assert task_approval.risk_level == "medium"
    assert task_approval.tool_call_id == scripts.SUBAGENT_TASK_CALL_ID
    run = await latest_run(db_session, created.id)
    sandbox = approval_env.sandboxes[created.id]
    assert sandbox.tool_commands == []

    # 批准 task → 续跑；coder 内 execute 触发第二挂起
    first = await async_client.post(
        f"{APPROVALS}/{task_approval.id}/decide", json={"decision": "approve"}
    )
    assert first.status_code == 200, first.text
    rows = await wait_for_approvals(db_session, created.id, count=2)
    await wait_for_events(db_session, created.id, "approval_request", count=2)
    exec_approval = rows[1]
    assert (exec_approval.tool, exec_approval.status) == ("execute", "pending")
    assert exec_approval.tool_call_id == scripts.SUBAGENT_EXECUTE_CALL_ID
    assert exec_approval.args == {"command": scripts.APPROVAL_COMMAND}
    await wait_run_state(async_client, created.id, "waiting_approval")
    assert sandbox.tool_commands == [], "execute 审批通过前命令不得执行（沙箱红线）"

    # 刷新恢复只看 pending：只剩 execute 一张卡
    page = await async_client.get(f"{APPROVALS}?status=pending&session_id={created.id}")
    items = unwrap(page)["items"]
    assert [item["id"] for item in items] == [str(exec_approval.id)]

    # 批准 execute → coder 执行 → 全链收口
    second = await async_client.post(
        f"{APPROVALS}/{exec_approval.id}/decide", json={"decision": "approve"}
    )
    assert second.status_code == 200, second.text
    await wait_run_state(async_client, created.id, "idle")

    assert sandbox.tool_commands == [scripts.APPROVAL_COMMAND], "命令恰好执行一次（不重复）"

    run_row = await db_session.get(type(run), run.id)
    await db_session.refresh(run_row)
    assert run_row.status == "completed"

    # 事件：两次 approval_request（task → execute），两次 approval_resolved
    events = await events_of(db_session, created.id)
    requests = event_payload(events, "approval_request")
    assert [payload["tool"] for payload in requests] == ["task", "execute"]
    assert len(event_payload(events, "approval_resolved")) == 2

    # usage_totals：三段合计 12/4 + 9/3 + (7/2 + 18/6) = 46/15；model_calls 不重复
    finished = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") == "finished"
    ]
    assert len(finished) == 1
    assert Usage.model_validate(finished[0]["usage_totals"]) == Usage(
        input_tokens=46, output_tokens=15
    )
    calls = await model_calls_of(db_session, run.id)
    assert sum(call.input_tokens for call in calls) == 46
    assert sum(call.output_tokens for call in calls) == 15
    assert len(calls) == 4, "每次模型调用一行（主 2 + coder 2），resume 不重复记账"

    # 审批行留痕：task/execute 均 approved
    rows = await approvals_of(db_session, created.id)
    assert [(row.tool, row.status, row.decided_by) for row in rows] == [
        ("task", "approved", "user"),
        ("execute", "approved", "user"),
    ]


async def test_subagent_pending_approvals_survive_refresh_before_decide(
    async_client, approval_env, db_session
) -> None:
    """挂起态跨请求存活（AC-006-5）：第一个挂起不决定，直接再看列表仍返回同一行。"""
    created = await create_session(async_client, title="刷新恢复")
    approval_env.set_role_models(
        {
            "main": ScriptedChatModel(steps=scripts.subagent_task_then_reply()),
            "coder": ScriptedChatModel(steps=scripts.subagent_execute()),
        }
    )
    await post_message(async_client, created.id, "派 coder")
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=1)
    await wait_for_events(db_session, created.id, "approval_request")

    first = unwrap(await async_client.get(f"{APPROVALS}?status=pending&session_id={created.id}"))
    second = unwrap(await async_client.get(f"{APPROVALS}?status=pending&session_id={created.id}"))
    assert [item["id"] for item in first["items"]] == [str(rows[0].id)]
    assert [item["id"] for item in second["items"]] == [str(rows[0].id)]
    assert first["items"][0]["reason"], "恢复卡片必须带规则理由（AC-006-5）"

    # 收尾：拒绝让 run 正常结束（避免留挂起态给下一测的池）
    reject = await async_client.post(
        f"{APPROVALS}/{rows[0].id}/decide", json={"decision": "reject"}
    )
    assert reject.status_code == 200, reject.text
    await wait_run_state(async_client, created.id, "idle")


async def test_unknown_approval_id_is_404(async_client, approval_env, db_session) -> None:  # noqa: ARG001
    """未知审批 id → 404（契约 APPROVAL_NOT_FOUND）。"""
    response = await async_client.get(f"{APPROVALS}/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "APPROVAL_NOT_FOUND"
