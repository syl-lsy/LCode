"""「0 绕审」不变量 integration（M3 硬判据，真 PG）。

PRD 成功标准 #4 / 里程碑 M3：「危险操作 0 次绕过审批执行」。本文件把这句话落成可独立
复核的机器判据——**工具执行证据**（FakeSandbox 记录的带 `exec_id` 的副作用）与
**审批决策行**（approvals 表）做集合相等核对：

    执行集合 == { approve, edit 决策行 }   （沙箱红线工具范围内）

- 每条执行都必须有决策（不存在「执行了但没有审批行」→ 绕审）；
- 每条 approve/edit 都必须对应一次执行（不存在「批准了但工具没跑」的假审计）；
- `auto_approved` 只允许出现在非红线工具（AUTO 分类器），红线工具出现即违规；
- 同一 tool_call_id 不允许多条决策行（伪造/重复留痕）。

`exec_id` 的来源是 tap 中间件写入的 `EXEC_ID_CONTEXT`：只在工具调用期间有值，run 收口
后的 diff 链 git 命令读到 None——所以「工具执行」与「系统维护命令」在证据面上天然可分。

反向自检（`test_cross_check_detects_fabricated_audit`）：故意构造「已执行但删掉审批行」
「凭空多一条审批行」「重复决策行」三种伪造场景，断言核对函数能逐一报出来。没有这条
自检，上面的等式可能因为两边都为空而变成空检查。

替身边界：模型=ScriptedChatModel、沙箱=FakeSandbox（真 tmp + 真子进程）、
checkpointer=InMemorySaver；事件管线/DB 走真件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.agent.permissions import SANDBOX_REDLINE_TOOLS
from app.models import Approval
from lcode_contracts.rest import SessionDetail
from test.support import scripts
from test.support.approval_env import (
    APPROVALS,
    approvals_of,
    create_session,
    events_of,
    latest_run,
    post_message,
    wait_for_approvals,
    wait_for_events,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel, usage_of
from test.support.fake_sandbox import FakeSandbox

pytestmark = pytest.mark.requires_db

REDLINE = SANDBOX_REDLINE_TOOLS
"""危险工具集合（生产常量；测试不另立名单，避免两处漂移）。"""

EXECUTED_STATUSES: frozenset[str] = frozenset({"approved", "edited"})
"""会导致工具真实执行的决策（reject/respond 不执行；auto_approved 只服务非红线）。"""

BLOCKED_PATH = "/blocked.txt"
PROTECTED_PATH = "/protected.txt"
PROTECTED_BEFORE = "hello\n"
PROTECTED_AFTER = "pwned\n"


# --------------------------------------------------------------------------- 证据模型


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """一次**工具触发的**沙箱副作用（exec_id 非空；kind=command|upload）。"""

    tool_call_id: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """一条审批决策行（approvals 表的证据投影）。"""

    tool_call_id: str
    tool: str
    status: str


def cross_check(executions: list[ExecutionRecord], decisions: list[DecisionRecord]) -> list[str]:
    """执行证据 × 决策行的交叉核对；返回违规说明列表（空 = 一致）。

    集合语义（不是计数语义）：
    - 执行集合 = 带 exec_id 的副作用来源 tool_call_id；
    - 批准集合 = 红线工具上 status ∈ {approved, edited} 的 tool_call_id；
    - 两者必须相等；任一方向的差集都是违规。
    """
    violations: list[str] = []
    executed = {record.tool_call_id for record in executions if record.tool_call_id}
    approved = {
        record.tool_call_id
        for record in decisions
        if record.tool in REDLINE and record.status in EXECUTED_STATUSES
    }

    for call_id in sorted(executed - approved):
        violations.append(f"EXECUTED_WITHOUT_DECISION:{call_id}")
    for call_id in sorted(approved - executed):
        violations.append(f"DECISION_WITHOUT_EXECUTION:{call_id}")

    for record in decisions:
        if record.tool in REDLINE and record.status == "auto_approved":
            violations.append(f"AUTO_APPROVED_REDLINE:{record.tool_call_id}")

    seen: set[str] = set()
    for record in decisions:
        key = f"{record.tool_call_id}:{record.status}"
        if record.tool_call_id in seen:
            violations.append(f"DUPLICATE_DECISION_ROW:{key}")
        seen.add(record.tool_call_id)
    return violations


# --------------------------------------------------------------------------- 场景剧本


def _script_all_rejected() -> list[AIMessage]:
    """同一 turn 三个危险调用（execute + write_file + edit_file）→ 全部人工决策。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": "printf 'lcode-should-not-run\\n'"},
                    "id": "call-nb-exec",
                    "type": "tool_call",
                },
                {
                    "name": "write_file",
                    "args": {"file_path": BLOCKED_PATH, "content": "must not exist\n"},
                    "id": "call-nb-write",
                    "type": "tool_call",
                },
                {
                    "name": "edit_file",
                    "args": {
                        "file_path": PROTECTED_PATH,
                        "old_string": PROTECTED_BEFORE,
                        "new_string": PROTECTED_AFTER,
                    },
                    "id": "call-nb-edit",
                    "type": "tool_call",
                },
            ],
            usage_metadata=usage_of(input_tokens=12, output_tokens=6),
        ),
        AIMessage(
            content="全部决策后收口。", usage_metadata=usage_of(input_tokens=6, output_tokens=2)
        ),
    ]


def _script_mixed() -> list[AIMessage]:
    """正向对照：execute + write_file 批准、edit_file 驳回（证明等式两侧非空）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": "printf 'lcode-approved-exec\\n'"},
                    "id": "call-mixed-exec",
                    "type": "tool_call",
                },
                {
                    "name": "write_file",
                    "args": {"file_path": BLOCKED_PATH, "content": "approved write\n"},
                    "id": "call-mixed-write",
                    "type": "tool_call",
                },
                {
                    "name": "edit_file",
                    "args": {
                        "file_path": PROTECTED_PATH,
                        "old_string": PROTECTED_BEFORE,
                        "new_string": PROTECTED_AFTER,
                    },
                    "id": "call-mixed-edit",
                    "type": "tool_call",
                },
            ],
            usage_metadata=usage_of(input_tokens=12, output_tokens=6),
        ),
        AIMessage(
            content="决策完成后收口。", usage_metadata=usage_of(input_tokens=6, output_tokens=2)
        ),
    ]


# --------------------------------------------------------------------------- 助手


def _workspace(tmp_path: Path, session_id: str) -> Path:
    """会话工作区（ApprovalEnv 的 FakeSandbox root 口径：tmp_path/session_id）。"""
    root = tmp_path / session_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _executions(sandbox: FakeSandbox) -> list[ExecutionRecord]:
    return [
        ExecutionRecord(tool_call_id=effect.exec_id, kind=effect.kind, detail=effect.detail)
        for effect in sandbox.side_effects
        if effect.exec_id
    ]


def _decisions(rows: list[Approval]) -> list[DecisionRecord]:
    return [
        DecisionRecord(tool_call_id=row.tool_call_id, tool=row.tool, status=row.status)
        for row in rows
    ]


def _assert_no_bypass(sandbox: FakeSandbox, rows: list[Approval]) -> None:
    """核心不变量：执行集合与决策集合相等（违规时打印全部证据）。"""
    executions = _executions(sandbox)
    decisions = _decisions(rows)
    violations = cross_check(executions, decisions)
    assert violations == [], (
        f"「0 绕审」不变量被破坏：{violations}\n"
        f"执行证据={[(item.tool_call_id, item.kind, item.detail) for item in executions]}\n"
        f"决策行={[(item.tool_call_id, item.tool, item.status) for item in decisions]}"
    )


async def _park(client, env, content: str) -> SessionDetail:
    created = await create_session(client, title="0 绕审")
    env.set_model(ScriptedChatModel(steps=_script_all_rejected()))
    response = await post_message(client, created.id, content)
    assert response.status_code == 200, response.text
    await wait_run_state(client, created.id, "waiting_approval")
    return created


async def _decide(client, approval_id: uuid.UUID, **payload: object) -> None:
    response = await client.post(f"{APPROVALS}/{approval_id}/decide", json=payload)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- 用例


async def test_batch_dangerous_calls_rejected_execute_nothing(
    async_client, approval_env, db_session, tmp_path
) -> None:
    """整批危险调用全部 reject：沙箱零执行、文件零改动、每条执行都有决策行（空集相等）。"""
    created = await _park(async_client, approval_env, "跑三个危险动作（全部拒绝）")
    ws = _workspace(tmp_path, created.id)
    (ws / "protected.txt").write_text(PROTECTED_BEFORE, encoding="utf-8")

    rows = await wait_for_approvals(db_session, created.id, count=3)
    await wait_for_events(db_session, created.id, "approval_request", count=3)
    sandbox = approval_env.sandboxes[created.id]

    assert [(row.tool, row.status) for row in rows] == [
        ("execute", "pending"),
        ("write_file", "pending"),
        ("edit_file", "pending"),
    ], "三个危险工具必须全部停在 pending（审批是唯一闸门）"
    # 决策前：没有任何工具副作用
    assert _executions(sandbox) == [], "审批前不得有任何工具侧沙箱副作用"
    assert sandbox.tool_commands == []
    assert not (ws / BLOCKED_PATH.lstrip("/")).exists()
    assert (ws / "protected.txt").read_text(encoding="utf-8") == PROTECTED_BEFORE

    for row in rows:
        await _decide(async_client, row.id, decision="reject", feedback="不执行")
    await wait_run_state(async_client, created.id, "idle")

    # 决策后：仍然零执行（reject 不触发工具）
    assert _executions(sandbox) == []
    assert sandbox.tool_commands == [], f"被拒命令不得执行：{sandbox.commands}"
    assert not (ws / BLOCKED_PATH.lstrip("/")).exists(), "被拒 write_file 不得落盘"
    assert (ws / "protected.txt").read_text(encoding="utf-8") == PROTECTED_BEFORE, (
        "被拒 edit_file 不得修改文件"
    )

    final_rows = await approvals_of(db_session, created.id)
    assert [row.status for row in final_rows] == ["rejected"] * 3
    _assert_no_bypass(sandbox, final_rows)

    # 事件面同样不得出现成功收口（reject 落 error ToolMessage）
    finished = [
        event.data
        for event in await events_of(db_session, created.id)
        if event.type == "tool_call_finished"
    ]
    assert [payload["status"] for payload in finished] == ["error", "error", "error"]

    run = await latest_run(db_session, created.id)
    assert run.status == "completed"


async def test_stop_while_pending_executes_nothing(
    async_client, approval_env, db_session, tmp_path
) -> None:
    """挂起中直接 stop：pending 全部 rejected(system)、沙箱零执行；之后决策 409。"""
    created = await _park(async_client, approval_env, "跑三个危险动作（然后停止）")
    ws = _workspace(tmp_path, created.id)
    (ws / "protected.txt").write_text(PROTECTED_BEFORE, encoding="utf-8")

    rows = await wait_for_approvals(db_session, created.id, count=3)
    await wait_for_events(db_session, created.id, "approval_request", count=3)
    run = await latest_run(db_session, created.id)
    sandbox = approval_env.sandboxes[created.id]

    response = await async_client.post(f"/api/v1/runs/{run.id}/stop")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "stopped"
    await wait_run_state(async_client, created.id, "idle")

    final_rows = await approvals_of(db_session, created.id)
    assert [(row.status, row.decision, row.decided_by) for row in final_rows] == [
        ("rejected", "reject", "system")
    ] * 3
    assert _executions(sandbox) == []
    assert sandbox.tool_commands == []
    assert not (ws / BLOCKED_PATH.lstrip("/")).exists()
    assert (ws / "protected.txt").read_text(encoding="utf-8") == PROTECTED_BEFORE
    _assert_no_bypass(sandbox, final_rows)

    late = await async_client.post(f"{APPROVALS}/{rows[0].id}/decide", json={"decision": "approve"})
    assert late.status_code == 409, late.text
    # 迟到的 approve 不得补出执行（行已 rejected(system)，resume 不再发生）
    assert _executions(sandbox) == []

    terminal = [
        event.data
        for event in await events_of(db_session, created.id)
        if event.type == "run_status" and event.data.get("phase") == "stopped"
    ]
    assert len(terminal) == 1


async def test_approved_calls_execute_exactly_once_and_rejected_do_not(
    async_client, approval_env, db_session, tmp_path
) -> None:
    """正向对照（让集合相等非空）：execute+write_file 批准各执行一次；edit_file 驳回不执行。"""
    created = await create_session(async_client, title="0 绕审-正向对照")
    approval_env.set_model(ScriptedChatModel(steps=_script_mixed()))
    ws = _workspace(tmp_path, created.id)
    (ws / "protected.txt").write_text(PROTECTED_BEFORE, encoding="utf-8")

    response = await post_message(async_client, created.id, "执行并写文件，但别改受保护文件")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=3)
    await wait_for_events(db_session, created.id, "approval_request", count=3)
    by_tool = {row.tool: row for row in rows}

    await _decide(async_client, by_tool["execute"].id, decision="approve")
    await _decide(async_client, by_tool["write_file"].id, decision="approve")
    await _decide(async_client, by_tool["edit_file"].id, decision="reject")
    await wait_run_state(async_client, created.id, "idle")

    sandbox = approval_env.sandboxes[created.id]
    executions = _executions(sandbox)
    assert {item.tool_call_id for item in executions} == {
        "call-mixed-exec",
        "call-mixed-write",
    }, f"只有被批准的两次调用能产生执行证据：{executions}"
    # 被批准的命令恰好执行一次；被驳回的工具没有产生任何命令
    approved_commands = [
        command for command in sandbox.tool_commands if command == "printf 'lcode-approved-exec\\n'"
    ]
    assert approved_commands == ["printf 'lcode-approved-exec\\n'"], "命令恰好执行一次"
    assert not any("lcode-should-not-run" in command for command in sandbox.commands)
    assert (ws / BLOCKED_PATH.lstrip("/")).read_text(encoding="utf-8") == "approved write\n"
    assert (ws / "protected.txt").read_text(encoding="utf-8") == PROTECTED_BEFORE, (
        "被驳回的 edit_file 不得改文件"
    )

    final_rows = await approvals_of(db_session, created.id)
    assert [(row.tool, row.status) for row in final_rows] == [
        ("execute", "approved"),
        ("write_file", "approved"),
        ("edit_file", "rejected"),
    ]
    _assert_no_bypass(sandbox, final_rows)

    # 命令类执行证据的归属：被批准 execute 的调用 id 在案，被驳回 edit 的 id 不在案
    command_call_ids = {item.tool_call_id for item in executions if item.kind == "command"}
    assert "call-mixed-exec" in command_call_ids
    assert "call-mixed-edit" not in command_call_ids


async def test_auto_mode_audits_non_redline_and_never_approves_redline(
    async_client, approval_env, db_session
) -> None:
    """AUTO：task 留 auto_approved 审计行（非红线），coder 的 execute 仍人工拒绝且零执行。"""
    created = await create_session(async_client, title="0 绕审-AUTO", approval_mode="auto")
    approval_env.set_role_models(
        {
            "main": ScriptedChatModel(steps=scripts.subagent_task_then_reply()),
            "coder": ScriptedChatModel(steps=scripts.subagent_execute()),
        }
    )

    response = await post_message(async_client, created.id, "派 coder 跑一条命令")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "waiting_approval")
    rows = await wait_for_approvals(db_session, created.id, count=2)
    await wait_for_events(db_session, created.id, "approval_request")
    sandbox = approval_env.sandboxes[created.id]

    execute_row = next(row for row in rows if row.tool == "execute")
    audit_row = next(row for row in rows if row.tool == "task")
    assert (audit_row.status, audit_row.decided_by) == ("auto_approved", "classifier")
    assert audit_row.reason and "AUTO" in audit_row.reason
    assert (execute_row.tool, execute_row.status) == ("execute", "pending")
    assert _executions(sandbox) == [], "AUTO 不得自动放行沙箱红线（execute 仍挂起）"

    await _decide(async_client, execute_row.id, decision="reject")
    await wait_run_state(async_client, created.id, "idle")

    final_rows = await approvals_of(db_session, created.id)
    assert [(row.tool, row.status) for row in final_rows] == [
        ("task", "auto_approved"),
        ("execute", "rejected"),
    ]
    assert _executions(sandbox) == []
    assert sandbox.tool_commands == []
    _assert_no_bypass(sandbox, final_rows)

    # 审计行只允许非红线（cross_check 已含此判据；此处再显式点名）
    assert all(row.tool not in REDLINE for row in final_rows if row.status == "auto_approved")


# --------------------------------------------------------------------------- 反向自检


def test_cross_check_detects_fabricated_audit() -> None:
    """核对函数的鉴别力：删审批行 / 伪造审批行 / 重复决策行都必须报违规。"""
    executions = [
        ExecutionRecord("call-exec", "command", "echo x"),
        ExecutionRecord("call-write", "upload", "/a.txt"),
    ]
    decisions = [
        DecisionRecord("call-exec", "execute", "approved"),
        DecisionRecord("call-write", "write_file", "approved"),
    ]
    assert cross_check(executions, decisions) == []

    # 伪造 1：执行了但删掉审批行 → 必须报「执行无决策」
    assert cross_check(executions, decisions[1:]) == ["EXECUTED_WITHOUT_DECISION:call-exec"]

    # 伪造 2：凭空多一条 approve（没执行）→ 必须报「决策无执行」
    fabricated = [*decisions, DecisionRecord("call-ghost", "edit_file", "approved")]
    assert cross_check(executions, fabricated) == ["DECISION_WITHOUT_EXECUTION:call-ghost"]

    # 伪造 3：同一调用两条决策行 → 必须报重复
    duplicated = [*decisions, DecisionRecord("call-exec", "execute", "approved")]
    assert "DUPLICATE_DECISION_ROW:call-exec:approved" in cross_check(executions, duplicated)

    # 伪造 4：红线工具出现 auto_approved → 必须报违规
    auto_redline = [DecisionRecord("call-exec", "execute", "auto_approved")]
    assert "AUTO_APPROVED_REDLINE:call-exec" in cross_check([], auto_redline)

    # 伪造 5：respond（人工代答）不算执行 → 不得出现在批准集合
    assert cross_check([], [DecisionRecord("call-exec", "execute", "responded")]) == []
