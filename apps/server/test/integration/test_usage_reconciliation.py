"""integration：并行子智能体交错流式下的用量对账基准（M4 前置 D5；REQ-015 / AC-015-1）。

背景（`docs/arch/m2-closure-checklist.md` openItems #5、`m3-closure-checklist.md` §8.3）：
run 收口的残差对账基准若取 `RunOutcome.usage`（runtime 侧 messages 通道的**单消息窗口**），
而逐次记账取 translator（**per-agent 窗口** + updates 通道），两者在 M4 fan_out 并行
子智能体交错流式时会结构性分叉：单窗口按「当前消息 id」折叠，交错时同一条消息的累计
量会被切成多段重复折叠（inflated），收口于是「凭空补出一条对账行」——终态
`run_status.usage_totals`、DB 行集、真实用量三者互不相等。

场景设计（确定性）：
- 主智能体同一 turn 派两个 `task`（coder + explorer）→ `ToolNode` 并行执行 → 两个子
  智能体的流式块真交错；
- 假模型用**累计量方言**（`usage_every_chunk=True`：每个流式块都带该步 usage）——
  translator 的 `_absorb_usage` 明列支持该方言，也正是单窗口消费者会重复折叠的输入。

判据：
(a) 该 run 全部 `model_calls` 行 token 合计 == 终态 `run_status(finished).usage_totals`
    == 剧本真值（不凭空增减 token）；
(b) 成功收口不补对账行：残差行最多一条且非负——成功路径按定义为 0 条
    （行数 == 剧本模型调用次数）；
(c) 逐次记账语义不回归：每次调用一行、token 与剧本逐行对位、主智能体归属保留。
"""

import pytest
from langchain_core.messages import AIMessage

from app.models import Event as EventRow
from lcode_contracts.common import Usage
from test.support import scripts
from test.support.approval_env import (
    create_session,
    event_payload,
    events_of,
    latest_run,
    model_calls_of,
    post_message,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel, usage_of

pytestmark = pytest.mark.requires_db

MAIN_DISPATCH_USAGE = (12, 4)
CODER_STEP_USAGE = ((9, 3), (7, 3))
EXPLORER_STEP_USAGE = ((5, 2), (3, 2))
MAIN_REPLY_USAGE = (18, 6)

EXPECTED_ROWS: tuple[tuple[int, int], ...] = (
    MAIN_DISPATCH_USAGE,
    *CODER_STEP_USAGE,
    *EXPLORER_STEP_USAGE,
    MAIN_REPLY_USAGE,
)
"""剧本真值：主 2 次 + coder 2 次 + explorer 2 次（逐次记账必须一行一次且 token 对位）。"""

EXPECTED_USAGE = Usage(
    input_tokens=sum(row[0] for row in EXPECTED_ROWS),
    output_tokens=sum(row[1] for row in EXPECTED_ROWS),
)

READER_REPLY_TEXT = "只读步骤完成，汇报结果。"
MAIN_REPLY_TEXT = "子任务都完成了。"


def _dispatch_steps() -> list[AIMessage]:
    """主智能体：同一 turn 并行派 coder + explorer（ToolNode 并行 → 子智能体流交错）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "跑只读步骤 A", "subagent_type": "coder"},
                    "id": "call-usage-task-coder",
                    "type": "tool_call",
                },
                {
                    "name": "task",
                    "args": {"description": "跑只读步骤 B", "subagent_type": "explorer"},
                    "id": "call-usage-task-explorer",
                    "type": "tool_call",
                },
            ],
            usage_metadata=usage_of(*MAIN_DISPATCH_USAGE),
        ),
        AIMessage(
            content=MAIN_REPLY_TEXT,
            usage_metadata=usage_of(*MAIN_REPLY_USAGE),
        ),
    ]


def _reader_steps(step_usage: tuple[tuple[int, int], tuple[int, int]]) -> list[AIMessage]:
    """只读子智能体：ls（免审批）→ 汇报；两次模型调用各带 usage。"""
    first, second = step_usage
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": scripts.TOOL_LS_PATH},
                    "id": "call-usage-reader-ls",
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(*first),
        ),
        AIMessage(content=READER_REPLY_TEXT, usage_metadata=usage_of(*second)),
    ]


def _cumulative_model(step_usage: tuple[tuple[int, int], tuple[int, int]]) -> ScriptedChatModel:
    """累计量方言 + 逐字符分块 + 块间延时：两个子智能体并发时块级交错。"""
    return ScriptedChatModel(
        steps=_reader_steps(step_usage),
        chunk_size=1,
        delay_seconds=0.01,
        usage_every_chunk=True,
    )


async def test_interleaved_subagent_streaming_usage_has_single_source(
    async_client, approval_env, db_session
) -> None:
    created = await create_session(async_client, title="交错用量对账", approval_mode="auto")
    main = ScriptedChatModel(steps=_dispatch_steps())
    coder = _cumulative_model(CODER_STEP_USAGE)
    explorer = _cumulative_model(EXPLORER_STEP_USAGE)
    approval_env.set_role_models({"main": main, "coder": coder, "explorer": explorer})

    response = await post_message(async_client, created.id, "并行派两个子智能体跑只读步骤")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    run = await latest_run(db_session, created.id)
    assert run.status == "completed"

    rows = await model_calls_of(db_session, run.id)
    events = await events_of(db_session, created.id)
    finished = [
        payload
        for payload in event_payload(events, "run_status")
        if payload.get("phase") == "finished"
    ]
    assert len(finished) == 1
    event_totals = Usage.model_validate(finished[0]["usage_totals"])

    # (a) 事件 / DB 行 / 剧本真值三者同源（残差基准若取 runtime 单窗口，这里会 inflated）
    row_totals = Usage(
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
    )
    assert row_totals == event_totals == EXPECTED_USAGE

    # 逐消息用量（translator 真值）也不受交错影响：收口事件里 6 条 message_finished 合计即真值
    message_totals = Usage(
        input_tokens=sum(payload["usage"]["input_tokens"] for payload in _message_usage(events)),
        output_tokens=sum(payload["usage"]["output_tokens"] for payload in _message_usage(events)),
    )
    assert message_totals == EXPECTED_USAGE

    # (b) 成功路径不补对账行：行数 == 剧本调用次数（残差行「最多一条且非负」的更强形式）
    invocations = main.invocations + coder.invocations + explorer.invocations
    assert invocations == len(EXPECTED_ROWS)
    assert len(rows) == invocations
    assert sorted((row.input_tokens, row.output_tokens) for row in rows) == sorted(EXPECTED_ROWS)
    assert all(row.input_tokens >= 0 and row.output_tokens >= 0 for row in rows)

    # (c) 既有语义不回归：一次调用一行（行数/逐行 token 已在上方对位）；主智能体首个
    # 派发回合发生在任何 task 入栈之前 → 必有行归主（agent=None）；两条 task 分支各自
    # 留下独立归属。注：并行 task 下 translator 的全局 task 栈会串味（第 2 条主智能体
    # 消息可能被标成子智能体 role），属归属（AC-016-2）问题，与本次用量对账无关，不在此锁定。
    main_rows = [row for row in rows if row.agent_id is None]
    subagent_ids = {row.agent_id for row in rows if row.agent_id is not None}
    assert len(main_rows) >= 1 and {row.role for row in main_rows} == {"main"}
    assert len(subagent_ids) >= 2  # 两条 task 各自独立归属


def _message_usage(events: list[EventRow]) -> list[dict]:
    """带 usage 的 message_finished 载荷（逐次记账的事件侧真值）。"""
    return [
        payload
        for payload in event_payload(events, "message_finished")
        if payload.get("usage") is not None
    ]
