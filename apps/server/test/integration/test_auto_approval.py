"""AUTO 审批分类器 integration（M3 Phase 3，真 PG；REQ-013 AC-013-3 / AC-013-4）。

覆盖（与单测互补的真图/真库面）：

1. **AUTO 放行 task、挂起在子智能体 execute**：`subagent_task_then_reply` 剧本下首次
   挂起必须是 coder 的 `execute`（不是 `task`）；`task` 不产生 pending 行，而产生
   一条 `status=auto_approved` / `decided_by=classifier` / 非空 reason 的审计行；
   批准 execute 后全链收口，resume 不重复落审计、不把 task 再打断。
2. **MANUAL 反向**：同剧本下 task 仍弹审批（先 task 后 execute 两条 pending），
   且没有任何 auto_approved 行。
3. **沙箱红线**：AUTO 下 execute/write_file/edit_file 仍全部产生 pending 行，
   且没有任何 auto_approved 行（AC-013-4）。

替身边界同审批主链：模型=ScriptedChatModel、沙箱=FakeSandbox、checkpointer=
InMemorySaver；事件管线/DB 走真件。
"""

import pytest
from langchain_core.messages import AIMessage

from lcode_contracts.rest import SessionDetail
from test.support import scripts
from test.support.approval_env import (
    APPROVALS,
    approvals_of,
    create_session,
    event_payload,
    events_of,
    latest_run,
    post_message,
    wait_for_approvals,
    wait_for_events,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel, usage_of

pytestmark = pytest.mark.requires_db


def _redline_script() -> list[AIMessage]:
    """AUTO 红线剧本：同一 turn 三个沙箱红线工具（execute/write_file/edit_file）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": scripts.APPROVAL_COMMAND},
                    "id": "call-auto-redline-exec",
                    "type": "tool_call",
                },
                {
                    "name": "write_file",
                    "args": {"file_path": "/auto-redline.txt", "content": "x"},
                    "id": "call-auto-redline-write",
                    "type": "tool_call",
                },
                {
                    "name": "edit_file",
                    "args": {
                        "file_path": "/auto-redline.txt",
                        "old_string": "x",
                        "new_string": "y",
                    },
                    "id": "call-auto-redline-edit",
                    "type": "tool_call",
                },
            ],
            usage_metadata=usage_of(input_tokens=11, output_tokens=5),
        ),
        AIMessage(
            content="全部拒绝后收口。", usage_metadata=usage_of(input_tokens=6, output_tokens=2)
        ),
    ]


async def _auto_session(client) -> SessionDetail:
    return await create_session(client, title="AUTO 分类器", approval_mode="auto")


async def test_auto_approves_task_and_parks_on_coder_execute(
    async_client, approval_env, db_session
) -> None:
    """AUTO：task 免审 + 审计行；首次挂起 = coder execute；resume 不重复落审计。"""
    created = await _auto_session(async_client)
    approval_env.set_role_models(
        {
            "main": ScriptedChatModel(steps=scripts.subagent_task_then_reply()),
            "coder": ScriptedChatModel(steps=scripts.subagent_execute()),
        }
    )

    response = await post_message(async_client, created.id, "派 coder 跑一条命令")
    assert response.status_code == 200, response.text

    # 首次挂起：coder 的 execute（task 已被分类器免审）
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=2)
    await wait_for_events(db_session, created.id, "approval_request")
    sandbox = approval_env.sandboxes[created.id]
    assert sandbox.tool_commands == [], "execute 审批通过前命令不得执行（沙箱红线）"

    pending = [row for row in rows if row.status == "pending"]
    assert [(row.tool, row.tool_call_id) for row in pending] == [
        ("execute", scripts.SUBAGENT_EXECUTE_CALL_ID)
    ], "AUTO 下首次挂起必须是 coder 的 execute，task 不得产生 pending 行"

    # task 的 AUTO 审计行（AC-013-3：status/decided_by/reason/risk 全量留痕）
    audited = [row for row in rows if row.status == "auto_approved"]
    assert len(audited) == 1
    task_row = audited[0]
    assert task_row.tool == "task"
    assert task_row.tool_call_id == scripts.SUBAGENT_TASK_CALL_ID
    assert task_row.decided_by == "classifier"
    assert task_row.decision is None
    assert task_row.reason and "AUTO" in task_row.reason
    assert task_row.risk_level == "medium"
    assert task_row.args == {"description": "coder 跑一条命令", "subagent_type": "coder"}

    # 事件面：只对 execute 发 approval_request（audit 行不新增 wire 事件）
    events = await events_of(db_session, created.id)
    requests = event_payload(events, "approval_request")
    assert [payload["tool"] for payload in requests] == ["execute"]

    # 批准 execute → coder 执行 → task 返回 → 主智能体收尾
    approved = await async_client.post(
        f"{APPROVALS}/{pending[0].id}/decide", json={"decision": "approve"}
    )
    assert approved.status_code == 200, approved.text
    await wait_run_state(async_client, created.id, "idle")

    assert sandbox.tool_commands == [scripts.APPROVAL_COMMAND], "命令恰好执行一次（不重复）"
    run = await latest_run(db_session, created.id)
    assert run.status == "completed"

    # 审计幂等：resume 重放同一 tool_call 不重复落 auto_approved；task 也不再被打断
    final_rows = await approvals_of(db_session, created.id)
    final_audited = [row for row in final_rows if row.status == "auto_approved"]
    assert len(final_audited) == 1
    assert [(row.tool, row.status, row.decided_by) for row in final_rows] == [
        ("task", "auto_approved", "classifier"),
        ("execute", "approved", "user"),
    ]
    # approval_resolved 只有 execute 一条（审计行没有人工决策回声）
    resolved = event_payload(await events_of(db_session, created.id), "approval_resolved")
    assert [payload["decision"] for payload in resolved] == ["approve"]


async def test_manual_mode_still_gates_task(async_client, approval_env, db_session) -> None:
    """MANUAL 反向：同剧本下 task 先弹审批（两条 pending），且无 auto_approved 行。"""
    created = await create_session(async_client, title="MANUAL 对照", approval_mode="manual")
    approval_env.set_role_models(
        {
            "main": ScriptedChatModel(steps=scripts.subagent_task_then_reply()),
            "coder": ScriptedChatModel(steps=scripts.subagent_execute()),
        }
    )

    await post_message(async_client, created.id, "派 coder 跑一条命令")
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=1)
    await wait_for_events(db_session, created.id, "approval_request")
    assert [(row.tool, row.status) for row in rows] == [("task", "pending")]

    first = await async_client.post(
        f"{APPROVALS}/{rows[0].id}/decide", json={"decision": "approve"}
    )
    assert first.status_code == 200, first.text
    rows = await wait_for_approvals(db_session, created.id, count=2)
    await wait_for_events(db_session, created.id, "approval_request", count=2)
    assert [row.tool for row in rows] == ["task", "execute"]
    assert all(row.status in ("approved", "pending") for row in rows)

    second = await async_client.post(
        f"{APPROVALS}/{rows[1].id}/decide", json={"decision": "approve"}
    )
    assert second.status_code == 200, second.text
    await wait_run_state(async_client, created.id, "idle")

    final_rows = await approvals_of(db_session, created.id)
    assert all(row.status != "auto_approved" for row in final_rows), "MANUAL 不得产生自动放行"


async def test_auto_never_auto_approves_sandbox_redline_tools(
    async_client, approval_env, db_session
) -> None:
    """AC-013-4：AUTO 下 execute/write_file/edit_file 仍是 pending（无 auto_approved）。"""
    created = await _auto_session(async_client)
    approval_env.set_model(ScriptedChatModel(steps=_redline_script()))

    await post_message(async_client, created.id, "跑三个沙箱动作")
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=3)
    await wait_for_events(db_session, created.id, "approval_request")

    assert [(row.tool, row.status) for row in rows] == [
        ("execute", "pending"),
        ("write_file", "pending"),
        ("edit_file", "pending"),
    ]
    assert all(row.risk_level in ("medium", "high") for row in rows)
    assert not any(row.status == "auto_approved" for row in rows)
    assert approval_env.sandboxes[created.id].tool_commands == []

    # 全部拒绝 → run 正常收口（不留下挂起态）
    for row in rows:
        rejected = await async_client.post(
            f"{APPROVALS}/{row.id}/decide", json={"decision": "reject"}
        )
        assert rejected.status_code == 200, rejected.text
    await wait_run_state(async_client, created.id, "idle")

    final_rows = await approvals_of(db_session, created.id)
    assert [row.status for row in final_rows] == ["rejected", "rejected", "rejected"]
    assert not any(row.status == "auto_approved" for row in final_rows)
