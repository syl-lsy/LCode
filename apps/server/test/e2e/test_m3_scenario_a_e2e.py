"""M3 Phase 6 · 场景 A 全链路 e2e（真服务 + 假模型 + FakeSandbox + 真 PG checkpointer）。

链路（PRD 里程碑 M3 行 / `docs/arch/m3-plan.md` Phase 6）：

    预置 git 仓库（模拟已 clone）→ 第 1 轮计划（write_todos 3 步）
    → execute 审批（未批准时命令不执行）→ 批准执行（stdout 真出现）
    → write_file + edit_file 同批审批 → 批准后文件真的变了 → run 收口
    → diff_ready(round=1, baseline=HEAD) → 驳回 + 意见 → 自动返工 run（首条用户消息含反馈）
    → 第 2 轮按反馈改码 → diff_ready(round=2, 对照同一 HEAD) → 接受
    → diff_resolved(accepted) + git_baselines(accepted) + 沙箱内 tag lcode-baseline/round-2
    → 第 3 轮：新 diff 对照 accepted 基线（下一轮可用新基线）
    → 断线重放：全量事件逐条重建（不丢不重；待决策卡/todo/diff 面板数据可恢复）。

进程边界：本文件只经 HTTP/SSE 与子进程服务对话，**不 import app**（e2e 的立意）。
数据库事实（git_baselines）用独立 psycopg2 连接读已提交行（连接参数取 conftest 注入的
环境变量）；工作区文件与 git 事实用**独立 git 进程**读宿主机 tmp 目录（不经被测代码）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import psycopg2  # type: ignore[import-untyped]  # 无官方 stubs（同 integration conftest 口径）
import pytest

from test.e2e import test_chat_sse_e2e as chat_e2e
from test.support import scripts

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[4]
SANDBOX_ROOT = Path(
    os.environ.get("LCODE_E2E_SANDBOX_ROOT", str(ROOT / "tmp" / "e2e" / "sandboxes"))
)
"""与 `test/e2e/conftest.py` 的 `LCODE_E2E_SANDBOX_ROOT` 同一个根（会话 → 子目录）。"""

REVIEWS = "/api/v1/diff-reviews"

_RUN_TERMINAL: tuple[str, ...] = ("finished", "stopped", "error")


# ------------------------------------------------------------------ 宿主机事实（git / db）


def workspace_of(session_id: str) -> Path:
    """会话在宿主机的假沙箱工作区（FakeSandbox root / session_id）。"""
    return SANDBOX_ROOT / session_id


def _run_git(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, check=True)


def git_out(ws: Path, *args: str) -> str:
    return _run_git(ws, *args).stdout.strip()


def seed_repo(ws: Path) -> str:
    """把工作区做成「已 clone 的仓库」：两个文件 + README，一个初始提交。

    返回初始提交 sha（第 1/2 轮 diff 的对照基线）。
    """
    ws.mkdir(parents=True, exist_ok=True)
    _run_git(ws, "init", "-q")
    (ws / "calc.py").write_text(scripts.SCENARIO_A_CALC_BUGGY, encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (ws / "README.md").write_text(scripts.SCENARIO_A_README_BEFORE, encoding="utf-8")
    _run_git(ws, "add", "-A")
    _run_git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "initial")
    return git_out(ws, "rev-parse", "HEAD")


def db_rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """独立连接读已提交事实（模拟外部审计视角；不 import app）。"""
    connection = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
    )
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [item[0] for item in cursor.description or ()]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()


# ------------------------------------------------------------------ 事件小工具


def events_of_type(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == event_type]


def _is_run_terminal(event: dict[str, Any]) -> bool:
    return (
        event["type"] == "run_status"
        and event["subject"] == "run"
        and event["phase"] in _RUN_TERMINAL
    )


async def read_until_type(
    stream: chat_e2e.SSEStream, event_type: str, *, timeout: float = 90.0
) -> list[dict[str, Any]]:
    """读到指定类型事件（含命中事件）；其余事件原样保留在返回列表。"""
    return await stream.read_until(lambda event: event["type"] == event_type, timeout=timeout)


async def read_until_approval_batch(
    stream: chat_e2e.SSEStream, expected: int, *, timeout: float = 90.0
) -> list[dict[str, Any]]:
    """读到累计 `expected` 条 approval_request（同批多条逐条发事件）。"""
    collected: list[dict[str, Any]] = []
    deadline = monotonic() + timeout
    while len(events_of_type(collected, "approval_request")) < expected:
        remaining = deadline - monotonic()
        assert remaining > 0, f"未在 {timeout}s 内收到 {expected} 条 approval_request"
        collected.append(await stream.next_event(timeout=min(remaining, 30.0)))
    return collected


async def get_review(client: httpx.AsyncClient, review_id: str) -> dict[str, Any]:
    response = await client.get(f"{REVIEWS}/{review_id}")
    return chat_e2e._unwrap(response)


# ------------------------------------------------------------------ 用例


async def test_scenario_a_full_chain_rework_baseline_and_replay(
    api_client: httpx.AsyncClient,
) -> None:
    """场景 A：克隆（预置 git 仓库）→ 计划 → 审批 → 改码 → 测试 → diff → 驳回重做 → 接受。"""
    session = await chat_e2e.create_session(api_client)
    ws = workspace_of(session.id)
    head = seed_repo(ws)

    live: list[dict[str, Any]] = []
    async with chat_e2e.SSEStream(api_client, session.id) as stream:
        ack = await chat_e2e.send_message(api_client, session.id, scripts.SCENARIO_A_USER_TEXT)
        assert ack.run_id

        # ---------------------------------------------------------- 第 1 轮：计划 + 审批
        first = await read_until_type(stream, "approval_request", timeout=60)
        live += first
        requests = events_of_type(first, "approval_request")
        assert len(requests) == 1, f"第 1 段只应挂一条 execute：{requests}"
        request = requests[0]
        assert request["tool"] == "execute"
        assert request["args"] == {"command": scripts.SCENARIO_A_EXEC_COMMAND}
        assert request["risk_level"] == "high"
        assert request["tool_call_id"] == scripts.SCENARIO_A_ROUND1_EXEC_CALL_ID
        assert request["run_id"] == ack.run_id

        # AC-005-1：计划先于工具执行出现，且 3 步（≥3）
        todo_finished = [
            event
            for event in first
            if event["type"] == "tool_call_finished"
            and event["tool_call_id"] == "call-sa-round1-todo"
        ]
        assert len(todo_finished) == 1
        assert [item["status"] for item in todo_finished[0]["todos"]] == [
            "in_progress",
            "pending",
            "pending",
        ]
        assert [item["content"] for item in todo_finished[0]["todos"]] == [
            "执行测试确认当前失败",
            "修复 calc.add 的运算错误",
            "复跑测试并确认通过",
        ]

        # 审批硬闸门：未批准 → 命令没跑、输出没有、文件没变
        assert (ws / "calc.py").read_text(encoding="utf-8") == scripts.SCENARIO_A_CALC_BUGGY
        assert (ws / "README.md").read_text(encoding="utf-8") == scripts.SCENARIO_A_README_BEFORE
        assert not any(
            event["type"] == "sandbox_output" and event.get("exec_id") == request["tool_call_id"]
            for event in first
        ), "未批准前不得出现该命令的任何输出块"
        assert not any(
            event["type"] == "tool_call_finished"
            and event["tool_call_id"] == request["tool_call_id"]
            for event in first
        ), "未批准前工具不得收口（没有执行）"
        detail = await chat_e2e.get_session(api_client, session.id)
        assert detail.run_state == "waiting_approval"
        pending_page = await chat_e2e.list_approvals(
            api_client, status="pending", session_id=session.id
        )
        assert [item.id for item in pending_page.items] == [request["approval_id"]]

        # AC-006-5/AC-008-3：断线重放可恢复 pending 卡 + todo 快照（不重复 started）
        async with chat_e2e.SSEStream(api_client, session.id) as replay_pending:
            replayed_pending = await read_until_approval_batch(replay_pending, 1)
        replay_requests = events_of_type(replayed_pending, "approval_request")
        assert [item["approval_id"] for item in replay_requests] == [request["approval_id"]]
        replayed_exec_started = [
            event
            for event in replayed_pending
            if event["type"] == "tool_call_started" and event["tool"] == "execute"
        ]
        assert len(replayed_exec_started) == 1, "重放不得重复 started（同一 tool_call_id 只一次）"
        assert any(
            event["type"] == "tool_call_finished" and event["todos"] == todo_finished[0]["todos"]
            for event in replayed_pending
        ), "重放必须能重建 todo 快照"

        # 批准 execute（真 REST 决策）→ 命令真的执行（stdout 带标记、exit_code=0）
        await chat_e2e.decide_approval(api_client, request["approval_id"])
        second = await read_until_approval_batch(stream, 2, timeout=90)
        live += second
        batch = events_of_type(second, "approval_request")
        assert [(item["tool"], item["tool_call_id"]) for item in batch] == [
            ("write_file", scripts.SCENARIO_A_ROUND1_WRITE_CALL_ID),
            ("edit_file", scripts.SCENARIO_A_ROUND1_EDIT_CALL_ID),
        ]
        exec_output = [
            event
            for event in second
            if event["type"] == "sandbox_output"
            and event.get("exec_id") == scripts.SCENARIO_A_ROUND1_EXEC_CALL_ID
        ]
        assert scripts.SCENARIO_A_EXEC_STDOUT.strip() in "".join(
            event["data"] for event in exec_output
        ), "批准后命令必须真的执行并在终端可见输出"
        exec_finished = [
            event
            for event in second
            if event["type"] == "tool_call_finished"
            and event["tool_call_id"] == scripts.SCENARIO_A_ROUND1_EXEC_CALL_ID
        ]
        assert exec_finished and exec_finished[-1]["status"] == "success"
        assert exec_finished[-1]["exit_code"] == 0
        # 第二段审批尚未决策：文件仍未改
        assert (ws / "calc.py").read_text(encoding="utf-8") == scripts.SCENARIO_A_CALC_BUGGY

        for item in batch:
            await chat_e2e.decide_approval(api_client, item["approval_id"])

        tail = await stream.read_until(_is_run_terminal, timeout=90)
        live += tail
        assert tail[-1]["phase"] == "finished"
        ready_one = await read_until_type(stream, "diff_ready", timeout=60)
        live += ready_one
        round_one = events_of_type(ready_one, "diff_ready")[0]

        # AC-007-1：diff 就绪（round=1，对照输入仓库 HEAD）；totals 与 files 求和一致
        assert round_one["round"] == 1
        assert round_one["baseline"] == {"kind": "head", "ref": "HEAD", "commit": head}
        assert round_one["files"], "第 1 轮必须有文件改动"
        assert round_one["additions_total"] == sum(item["additions"] for item in round_one["files"])
        assert round_one["deletions_total"] == sum(item["deletions"] for item in round_one["files"])
        assert {item["path"] for item in round_one["files"]} == {"README.md", "calc.py"}
        changes = {item["path"]: item["change"] for item in round_one["files"]}
        assert changes == {"README.md": "modified", "calc.py": "modified"}

        # 行为级事实：批准后宿主文件真的变了（write_file / edit_file 都生效）
        assert (ws / "calc.py").read_text(encoding="utf-8") == scripts.SCENARIO_A_CALC_FIXED
        assert (ws / "README.md").read_text(encoding="utf-8") == scripts.SCENARIO_A_README_AFTER
        # 基线未推进（未被接受）
        assert git_out(ws, "rev-parse", "HEAD") == head
        assert git_out(ws, "tag", "--list") == ""

        # 逐文件 patch 从 REST 可读（审查面板数据面）
        patch = chat_e2e._unwrap(
            await api_client.get(f"{REVIEWS}/{round_one['review_id']}/files/calc.py")
        )
        assert patch["change"] == "modified"
        assert "+    return a + b" in patch["patch"]

        # ---------------------------------------------------------- 驳回 → 自动返工
        reject_response = await api_client.post(
            f"{REVIEWS}/{round_one['review_id']}/verdict",
            json={
                "verdict": "rejected",
                "feedback": f"请保留加法实现，并按意见补文档串（{scripts.SCENARIO_A_REWORK_KEYWORD}）。",
            },
        )
        assert reject_response.status_code == 200, reject_response.text
        assert chat_e2e._unwrap(reject_response)["verdict"] == "rejected"

        rejected_tail = await stream.read_until(
            lambda event: (
                event["type"] == "message_created"
                and event["role"] == "user"
                and scripts.SCENARIO_A_REWORK_KEYWORD in event["content"]
            ),
            timeout=60,
        )
        live += rejected_tail
        resolved_one = events_of_type(rejected_tail, "diff_resolved")
        assert [(item["round"], item["verdict"]) for item in resolved_one] == [(1, "rejected")]
        assert resolved_one[0]["decided_by"] == "user"
        rework_message = rejected_tail[-1]
        assert "上一轮改动未通过 diff 审查" in rework_message["content"]
        assert scripts.SCENARIO_A_REWORK_KEYWORD in rework_message["content"]

        # 第 2 轮（返工 run）：审批自动批准 → 收口 → diff_ready(round=2)
        turn_two = await chat_e2e.read_turn(stream, api_client, timeout=120)
        live += turn_two
        assert turn_two[-1]["phase"] == "finished"
        ready_two = await read_until_type(stream, "diff_ready", timeout=60)
        live += ready_two
        round_two = events_of_type(ready_two, "diff_ready")[0]
        assert round_two["round"] == 2
        # 第 1 轮被驳回 → 基线不推进：第 2 轮对照的仍是同一 commit
        assert round_two["baseline"] == round_one["baseline"]
        assert round_two["additions_total"] == sum(item["additions"] for item in round_two["files"])
        assert {item["path"]: item["change"] for item in round_two["files"]} == {
            "README.md": "modified",
            "calc.py": "modified",
        }
        assert (ws / "calc.py").read_text(encoding="utf-8") == scripts.SCENARIO_A_CALC_FINAL
        assert git_out(ws, "rev-parse", "HEAD") == head

        # ---------------------------------------------------------- 接受 → 基线推进
        accept_response = await api_client.post(
            f"{REVIEWS}/{round_two['review_id']}/verdict", json={"verdict": "accepted"}
        )
        assert accept_response.status_code == 200, accept_response.text
        accepted_detail = chat_e2e._unwrap(accept_response)
        assert accepted_detail["verdict"] == "accepted"
        assert accepted_detail["decided_by"] == "user"

        accepted_events = await read_until_type(stream, "diff_resolved", timeout=30)
        live += accepted_events
        accepted_event = events_of_type(accepted_events, "diff_resolved")[0]
        assert (accepted_event["round"], accepted_event["verdict"]) == (2, "accepted")

        # 沙箱内基线提交 + tag 真的存在（独立 git 进程核对）
        accepted_sha = git_out(ws, "rev-parse", "HEAD")
        assert accepted_sha != head
        assert git_out(ws, "log", "-1", "--pretty=%s").startswith("[lcode-baseline] round-2")
        assert "lcode-baseline/round-2" in git_out(ws, "tag", "--list").splitlines()
        assert git_out(ws, "rev-parse", "lcode-baseline/round-2") == accepted_sha

        # git_baselines(accepted) 行落库（外部连接读已提交事实）
        baselines = db_rows(
            "SELECT round, kind, ref, commit FROM git_baselines "
            "WHERE session_id = %s ORDER BY round",
            (session.id,),
        )
        assert [(row["round"], row["kind"], row["ref"], row["commit"]) for row in baselines] == [
            (0, "head", "HEAD", head),
            (2, "accepted", "lcode-baseline/round-2", accepted_sha),
        ]

        # ---------------------------------------------------------- 第 3 轮：新基线可用
        watermark = stream_last_seq(live)
        await chat_e2e.send_message(api_client, session.id, scripts.SCENARIO_A_THIRD_USER_TEXT)

        async with chat_e2e.SSEStream(
            api_client, session.id, last_event_id=watermark
        ) as third_stream:
            turn_three = await chat_e2e.read_turn(third_stream, api_client, timeout=120)
            assert turn_three[-1]["phase"] == "finished"
            ready_three_events = await read_until_type(third_stream, "diff_ready", timeout=60)
            turn_three += ready_three_events
            turn_three += await third_stream.drain(1.0)
        live += turn_three
        round_three = events_of_type(turn_three, "diff_ready")[0]
        assert round_three["round"] == 3
        assert round_three["baseline"] == {
            "kind": "accepted",
            "ref": "lcode-baseline/round-2",
            "commit": accepted_sha,
        }
        assert [item["path"] for item in round_three["files"]] == ["CHANGELOG.md"]

    # 注意：主订阅流（stream）在第三轮期间静默，队列里可能积压了第三轮事件；
    # 这些事件已由 third_stream 逐条消费，不能再 drain 主订阅（会重复计数）。

    # -------------------------------------------------------------- 断线重放（全量）
    last_seq = stream_last_seq(live)
    async with chat_e2e.SSEStream(api_client, session.id) as replay:
        replayed = await replay.read_until(lambda event: event["seq"] >= last_seq, timeout=120)
        replayed += await replay.drain(0.5)

    assert [event["seq"] for event in replayed] == list(range(last_seq + 1)), (
        "全量重放必须从 0 到水位连续（不丢不重）"
    )
    chat_e2e._assert_strictly_increasing(replayed)
    assert replayed == live, "重放事件必须与实时流逐字段一致（刷新可重建 UI 状态）"

    # 无重复 tool_call_started（按 run 分组；resume 播种去重的硬判据）
    started_keys: dict[tuple[str, str], int] = {}
    for event in replayed:
        if event["type"] == "tool_call_started":
            key = (str(event["run_id"]), event["tool_call_id"])
            started_keys[key] = started_keys.get(key, 0) + 1
    assert all(count == 1 for count in started_keys.values()), (
        f"存在重复 tool_call_started：{ {k: v for k, v in started_keys.items() if v > 1} }"
    )

    # 审批卡：每条 approval_request 唯一，且与 approvals 行一一对应（无重复审批行）
    request_events = events_of_type(replayed, "approval_request")
    request_ids = [event["approval_id"] for event in request_events]
    assert len(request_ids) == len(set(request_ids)) == 5, (
        f"5 条审批卡各出现一次：execute + write/edit 同批 2 条 + 返工 1 条 + 第 3 轮 1 条：{request_ids}"
    )
    approval_rows = db_rows(
        "SELECT id::text AS id, tool, status, tool_call_id FROM approvals "
        "WHERE session_id = %s ORDER BY created_at",
        (session.id,),
    )
    assert [row["id"] for row in approval_rows] == request_ids
    assert [row["status"] for row in approval_rows] == ["approved"] * 5
    assert [row["tool"] for row in approval_rows] == [
        "execute",
        "write_file",
        "edit_file",
        "write_file",
        "write_file",
    ]
    resolve_events = events_of_type(replayed, "approval_resolved")
    assert [event["approval_id"] for event in resolve_events] == request_ids

    # todo 快照可从重放恢复（面板数据源）
    todo_snapshots = [
        event["todos"]
        for event in replayed
        if event["type"] == "tool_call_finished" and event["todos"] is not None
    ]
    assert todo_snapshots and len(todo_snapshots[0]) == 3

    # diff 面板：三轮 ready 各一次；两轮裁决各一次；详情端点与事件逐字段一致
    ready_rounds = sorted(event["round"] for event in events_of_type(replayed, "diff_ready"))
    assert ready_rounds == [1, 2, 3]
    resolved_rounds = [
        (event["round"], event["verdict"]) for event in events_of_type(replayed, "diff_resolved")
    ]
    assert resolved_rounds == [(1, "rejected"), (2, "accepted")], (
        f"裁决回声必须一 reject 一 accept（各一次）：{resolved_rounds}"
    )

    review_rows = db_rows(
        "SELECT id::text AS id, round, verdict, additions_total, deletions_total "
        "FROM diff_reviews WHERE session_id = %s ORDER BY round",
        (session.id,),
    )
    assert [(row["round"], row["verdict"]) for row in review_rows] == [
        (1, "rejected"),
        (2, "accepted"),
        (3, "pending"),
    ]
    for row in review_rows:
        detail_payload = await get_review(api_client, row["id"])
        assert detail_payload["round"] == row["round"]
        assert detail_payload["additions_total"] == row["additions_total"]
        assert detail_payload["deletions_total"] == row["deletions_total"]
        assert detail_payload["additions_total"] == sum(
            item["additions"] for item in detail_payload["files"]
        )

    # 收尾无 pending 审批卡（没有留下无法决策的悬挂状态）
    final_pending = await chat_e2e.list_approvals(
        api_client, status="pending", session_id=session.id
    )
    assert final_pending.items == []


def stream_last_seq(events: list[dict[str, Any]]) -> int:
    """已消费事件的最后 seq（断线续传水位；空列表 = -1 语义由调用方保证非空）。"""
    assert events, "事件列表为空，无法取水位"
    return events[-1]["seq"]
