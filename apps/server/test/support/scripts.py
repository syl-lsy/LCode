"""剧本集（test-plan-m2 §3.2）：场景命名与三层（unit/integration/e2e）共用。

一个剧本 = 一组 `AIMessage` 步（`ScriptedChatModel.steps`）。同一剧本同时服务
translator unit 录制 golden、integration 真图与 e2e 进程内假模型（T7）——场景：

- SIMPLE_REPLY：单轮直答（integration 已用；e2e default/cost 路由复用）；
- TOOL_LOOP_TODO：write_todos → write_file → 收口（integration 已用）；
- MULTI_TURN：两步跨 run 消费（integration + e2e 刷新续聊共用）；
- STOP_STREAM：分块 + 块间延时的长文本（stop/cancel 场景）；
- LONG_500_TOKENS：≥500 token 剧本（AC-003-1 e2e `test_token_chunk_order`）；
- TOOL_EXECUTE：execute 工具真跑沙箱命令（AC-002-1 e2e 终端实时链）；
- TOOL_FAIL：execute 命中假沙箱不可用剧本（AC-004-4 e2e 失败工具如实呈现）。

命令型剧本的命令文本带 `lcode-…` 标记：e2e 假沙箱按 `ExecStep.match` 识别标记决定
走"多块流式输出 / 抛 SandboxUnavailable"剧本，其余命令照常真跑本地子进程——剧本
语义由命令文本本身携带，无需额外的进程间控制通道。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from test.support.fake_llm import usage_of

SIMPLE_USER_TEXT = "你好，请介绍一下你自己。"
SIMPLE_REPLY_TEXT = "你好！我是 LCode 编码助手。"

TOOL_TODO_ARGS = {
    "todos": [
        {"content": "创建 hello.txt", "status": "in_progress"},
        {"content": "汇报结果", "status": "pending"},
    ]
}
TOOL_FILE_PATH = "/hello.txt"
TOOL_FILE_CONTENT = "hello from lcode"

MULTI_TURN_FIRST_REPLY = "第一轮：收到。"
MULTI_TURN_SECOND_REPLY = "第二轮：我看到了我们之前聊的内容。"

STOP_STREAM_TEXT = "流式" * 80  # 320 字，chunk_size=8 → 40 块，给取消留足窗口

# ---- e2e 场景（T7）：命令标记与长文剧本 -----------------------------------------

LONG_TOKEN_COUNT = 600
"""长文剧本的 token 数（≥500，AC-003-1 的"≥500 token 剧本"判据）。"""

LONG_500_TEXT = " ".join(f"tok{i:03d}" for i in range(1, LONG_TOKEN_COUNT + 1))
"""600 个 6 字符 token 空格连接（4199 字符）；chunk_size=7 时恰好 600 块。"""

EXEC_STREAM_COMMAND = "echo lcode-stream-line-1; echo lcode-stream-line-2"
"""沙箱命令：命中假沙箱的多块流式输出剧本（AC-002-1 终端实时链）。"""

EXEC_STREAM_LINES = ("lcode-stream-line-1\n", "lcode-stream-line-2\n")
"""流式剧本的两条 stdout 块（顺序断言锚）。"""

EXEC_FAIL_COMMAND = "probe lcode-fail-sandbox"
"""沙箱命令：命中假沙箱的 SandboxUnavailable 剧本（AC-004-4 / AC-002-4）。"""

# ---- M3 审批场景：execute 门控与子智能体审批 -------------------------------------

APPROVAL_COMMAND = "echo lcode-approval"
"""审批命令：`echo` 不在只读白名单 → 恒触发 interrupt（AC-013-2）。"""

APPROVAL_TOOL_CALL_ID = "call-approval-exec"
APPROVAL_REPLY_TEXT = "审批流程结束。"

APPROVAL_BATCH_COMMANDS = (APPROVAL_COMMAND, "echo lcode-approval-2")
APPROVAL_BATCH_TOOL_CALL_IDS = ("call-approval-exec-1", "call-approval-exec-2")
"""并发批次：同一 model turn 两个 execute 调用 → 同一个 interrupt 里两条 action_request。"""

SUBAGENT_TASK_CALL_ID = "call-approval-task"
SUBAGENT_EXECUTE_CALL_ID = "call-approval-coder-exec"
SUBAGENT_REPLY_TEXT = "子智能体已完成任务。"


def approval_execute() -> list[AIMessage]:
    """审批主链：execute（触发审批）→ 批准后收口（两段各带 usage，锁跨段合计）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": APPROVAL_COMMAND},
                    "id": APPROVAL_TOOL_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=10, output_tokens=5),
        ),
        AIMessage(
            content=APPROVAL_REPLY_TEXT,
            usage_metadata=usage_of(input_tokens=20, output_tokens=8),
        ),
    ]


def approval_execute_batch() -> list[AIMessage]:
    """并发审批批次：同一 turn 两个 execute 调用（一个 interrupt 两条 action_request）。

    用于「同一批多个审批并发决策」用例：两条 pending 行同 run、同批次。
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": command},
                    "id": tool_call_id,
                    "type": "tool_call",
                }
                for command, tool_call_id in zip(
                    APPROVAL_BATCH_COMMANDS, APPROVAL_BATCH_TOOL_CALL_IDS, strict=True
                )
            ],
            usage_metadata=usage_of(input_tokens=10, output_tokens=5),
        ),
        AIMessage(
            content=APPROVAL_REPLY_TEXT,
            usage_metadata=usage_of(input_tokens=20, output_tokens=8),
        ),
    ]


def subagent_task_then_reply() -> list[AIMessage]:
    """主智能体：task 派 coder（MANUAL 下先审 task）→ 批准并等 coder 收口后回答。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "coder 跑一条命令", "subagent_type": "coder"},
                    "id": SUBAGENT_TASK_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=12, output_tokens=4),
        ),
        AIMessage(
            content=SUBAGENT_REPLY_TEXT,
            usage_metadata=usage_of(input_tokens=18, output_tokens=6),
        ),
    ]


def subagent_execute() -> list[AIMessage]:
    """coder 子智能体：execute（子图 interrupt → 顶层层恢复）→ 汇报。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": APPROVAL_COMMAND},
                    "id": SUBAGENT_EXECUTE_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=9, output_tokens=3),
        ),
        AIMessage(
            content="coder 完成。",
            usage_metadata=usage_of(input_tokens=7, output_tokens=2),
        ),
    ]


def long_500_tokens() -> list[AIMessage]:
    """LONG_500_TOKENS：一条 ≥500 token 的长回复（流式分块由模型侧 chunk_size 决定）。"""
    return [
        AIMessage(
            content=LONG_500_TEXT,
            usage_metadata=usage_of(input_tokens=120, output_tokens=LONG_TOKEN_COUNT),
        )
    ]


# ---- M3 场景 A 全链路（e2e）：计划 → 审批执行 → 改码 → diff → 驳回返工 → 接受 ----------

SCENARIO_A_USER_TEXT = "场景A首轮：修好 calc.add 的 bug，跑一下验证，然后给我看 diff。"
"""第 1 轮用户消息（路由关键词「场景A首轮」；不复用「第一轮」以免与 multi_turn 路由撞）。"""

SCENARIO_A_REWORK_KEYWORD = "场景A返工"
"""第 2 轮的驳回意见关键词（返工消息由 diff 审查服务生成，正文含反馈原文）。"""

SCENARIO_A_THIRD_USER_TEXT = "场景A收尾轮：再补一个 CHANGELOG，确认新基线可用。"

SCENARIO_A_TODO = {
    "todos": [
        {"content": "执行测试确认当前失败", "status": "in_progress"},
        {"content": "修复 calc.add 的运算错误", "status": "pending"},
        {"content": "复跑测试并确认通过", "status": "pending"},
    ]
}
"""第 1 轮计划：3 步（AC-005-1 的 ≥3 步判据）。"""

SCENARIO_A_EXEC_COMMAND = "printf 'lcode-scenario-a-exec-ok\\n'"
"""第 1 轮 execute：非只读白名单命令（printf）→ 必触发审批；stdout 留作执行证据。"""

SCENARIO_A_EXEC_STDOUT = "lcode-scenario-a-exec-ok\n"

SCENARIO_A_ROUND1_EXEC_CALL_ID = "call-sa-round1-exec"
SCENARIO_A_ROUND1_WRITE_CALL_ID = "call-sa-round1-write"
SCENARIO_A_ROUND1_EDIT_CALL_ID = "call-sa-round1-edit"
SCENARIO_A_ROUND2_WRITE_CALL_ID = "call-sa-round2-write"
SCENARIO_A_ROUND3_WRITE_CALL_ID = "call-sa-round3-write"

SCENARIO_A_CALC_BUGGY = "def add(a, b):\n    return a - b  # BUG: 应当是加法\n"
"""种子仓库里的错误实现（第 1 轮批准前必须原样不动）。"""

SCENARIO_A_CALC_FIXED = "def add(a, b):\n    return a + b\n"
"""第 1 轮 write_file 的目标内容。"""

SCENARIO_A_CALC_FINAL = (
    'def add(a, b):\n    """返回两数之和（按驳回意见补文档串）。"""\n    return a + b\n'
)
"""第 2 轮按驳回意见再改一版（diff 对抗同一基线的证据）。"""

SCENARIO_A_README_BEFORE = "# Demo\n\nA tiny calculator demo.\n"
SCENARIO_A_README_AFTER = "# Demo\n\nA tiny calculator demo.\n\nFixed by LCode scenario A.\n"
"""第 1 轮 edit_file 的目标内容（README 原文 → 追加一行）。"""

SCENARIO_A_CHANGELOG = "## round 3\n\n- baseline advanced after round-2 acceptance\n"

SCENARIO_A_ROUND1_REPLY = "第 1 轮完成：已修复 calc.add，等待 diff 审查。"
SCENARIO_A_ROUND2_REPLY = "第 2 轮完成：已按驳回意见修改。"
SCENARIO_A_ROUND3_REPLY = "第 3 轮完成：changelog 已补。"


def scenario_a_round_one() -> list[AIMessage]:
    """场景 A 第 1 轮：write_todos（3 步）→ execute（审批）→ write_file + edit_file（同批审批）→ 收口。

    两段工具循环各形成一次 HITL interrupt：第一段单条 execute，第二段同批两条
    （write_file/edit_file）——e2e 断言的批次语义由此覆盖。
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": dict(SCENARIO_A_TODO),
                    "id": "call-sa-round1-todo",
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=40, output_tokens=12),
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": SCENARIO_A_EXEC_COMMAND},
                    "id": SCENARIO_A_ROUND1_EXEC_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=60, output_tokens=18),
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "/calc.py", "content": SCENARIO_A_CALC_FIXED},
                    "id": SCENARIO_A_ROUND1_WRITE_CALL_ID,
                    "type": "tool_call",
                },
                {
                    "name": "edit_file",
                    "args": {
                        "file_path": "/README.md",
                        "old_string": SCENARIO_A_README_BEFORE,
                        "new_string": SCENARIO_A_README_AFTER,
                    },
                    "id": SCENARIO_A_ROUND1_EDIT_CALL_ID,
                    "type": "tool_call",
                },
            ],
            usage_metadata=usage_of(input_tokens=80, output_tokens=24),
        ),
        AIMessage(
            content=SCENARIO_A_ROUND1_REPLY,
            usage_metadata=usage_of(input_tokens=90, output_tokens=20),
        ),
    ]


def scenario_a_round_two() -> list[AIMessage]:
    """场景 A 第 2 轮（驳回返工）：按反馈再改 calc.py → 收口。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "/calc.py", "content": SCENARIO_A_CALC_FINAL},
                    "id": SCENARIO_A_ROUND2_WRITE_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=70, output_tokens=16),
        ),
        AIMessage(
            content=SCENARIO_A_ROUND2_REPLY,
            usage_metadata=usage_of(input_tokens=50, output_tokens=12),
        ),
    ]


def scenario_a_round_three() -> list[AIMessage]:
    """场景 A 第 3 轮（新基线可用）：新增 CHANGELOG → 收口。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "/CHANGELOG.md", "content": SCENARIO_A_CHANGELOG},
                    "id": SCENARIO_A_ROUND3_WRITE_CALL_ID,
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage_of(input_tokens=44, output_tokens=10),
        ),
        AIMessage(
            content=SCENARIO_A_ROUND3_REPLY,
            usage_metadata=usage_of(input_tokens=30, output_tokens=8),
        ),
    ]


def tool_execute() -> list[AIMessage]:
    """TOOL_EXECUTE：execute 工具真跑一条沙箱命令 → 收口（沙箱输出经事件桥直投 SSE）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": EXEC_STREAM_COMMAND},
                    "id": "call-exec-stream",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="命令执行完毕。", usage_metadata=usage_of(input_tokens=30, output_tokens=10)
        ),
    ]


def tool_fail() -> list[AIMessage]:
    """TOOL_FAIL：execute 命中不可用剧本 → 工具失败 → 模型如实汇报后收口。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute",
                    "args": {"command": EXEC_FAIL_COMMAND},
                    "id": "call-exec-fail",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="命令失败了，我如实汇报。",
            usage_metadata=usage_of(input_tokens=20, output_tokens=8),
        ),
    ]


def simple_reply() -> list[AIMessage]:
    """SIMPLE_REPLY：单轮直答（带 usage）。"""
    return [
        AIMessage(
            content=SIMPLE_REPLY_TEXT,
            usage_metadata=usage_of(input_tokens=12, output_tokens=7),
        )
    ]


def tool_loop_todo() -> list[AIMessage]:
    """TOOL_LOOP_TODO：write_todos → write_file → 收口（两段工具循环）。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": dict(TOOL_TODO_ARGS),
                    "id": "call-todo-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": TOOL_FILE_PATH, "content": TOOL_FILE_CONTENT},
                    "id": "call-file-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="两步都完成了。",
            usage_metadata=usage_of(input_tokens=40, output_tokens=15),
        ),
    ]


TOOL_LS_PATH = "/"
TOOL_LS_CALL_ID = "call-ls-1"


def tool_loop_read_only() -> list[AIMessage]:
    """READ_ONLY_LOOP：write_todos → ls → 收口（tap / 成本语义用例的免审批剧本）。

    M3 起 write_file/edit_file/execute 恒在审批面（沙箱红线），工具循环类用例若沿用
    写工具会停在 waiting_approval；本剧本用只读工具（ls）保留「真实工具执行 + 多次
    模型调用」的语义，usage 数值与 TOOL_LOOP_TODO 对齐（既有断言不变）。
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": dict(TOOL_TODO_ARGS),
                    "id": "call-todo-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ls",
                    "args": {"path": TOOL_LS_PATH},
                    "id": TOOL_LS_CALL_ID,
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="两步都完成了。",
            usage_metadata=usage_of(input_tokens=40, output_tokens=15),
        ),
    ]


def multi_turn() -> list[AIMessage]:
    """MULTI_TURN：两次 run 各消费一步（模型实例跨 run 复用）。"""
    return [
        AIMessage(
            content=MULTI_TURN_FIRST_REPLY,
            usage_metadata=usage_of(input_tokens=10, output_tokens=6),
        ),
        AIMessage(
            content=MULTI_TURN_SECOND_REPLY,
            usage_metadata=usage_of(input_tokens=25, output_tokens=9),
        ),
    ]


def stop_stream() -> list[AIMessage]:
    """STOP_STREAM：一条长文本（分块 + 块间延时），供 stop/cancel 场景打断。"""
    return [AIMessage(content=STOP_STREAM_TEXT, id="ai-stop-stream")]


__all__ = [
    "APPROVAL_COMMAND",
    "APPROVAL_REPLY_TEXT",
    "APPROVAL_TOOL_CALL_ID",
    "EXEC_FAIL_COMMAND",
    "EXEC_STREAM_COMMAND",
    "EXEC_STREAM_LINES",
    "LONG_500_TEXT",
    "LONG_TOKEN_COUNT",
    "MULTI_TURN_FIRST_REPLY",
    "MULTI_TURN_SECOND_REPLY",
    "SCENARIO_A_CALC_BUGGY",
    "SCENARIO_A_CALC_FINAL",
    "SCENARIO_A_CALC_FIXED",
    "SCENARIO_A_CHANGELOG",
    "SCENARIO_A_EXEC_COMMAND",
    "SCENARIO_A_EXEC_STDOUT",
    "SCENARIO_A_README_AFTER",
    "SCENARIO_A_README_BEFORE",
    "SCENARIO_A_REWORK_KEYWORD",
    "SCENARIO_A_ROUND1_EDIT_CALL_ID",
    "SCENARIO_A_ROUND1_EXEC_CALL_ID",
    "SCENARIO_A_ROUND1_REPLY",
    "SCENARIO_A_ROUND1_WRITE_CALL_ID",
    "SCENARIO_A_ROUND2_REPLY",
    "SCENARIO_A_ROUND2_WRITE_CALL_ID",
    "SCENARIO_A_ROUND3_REPLY",
    "SCENARIO_A_ROUND3_WRITE_CALL_ID",
    "SCENARIO_A_THIRD_USER_TEXT",
    "SCENARIO_A_TODO",
    "SCENARIO_A_USER_TEXT",
    "SIMPLE_REPLY_TEXT",
    "SIMPLE_USER_TEXT",
    "STOP_STREAM_TEXT",
    "SUBAGENT_EXECUTE_CALL_ID",
    "SUBAGENT_REPLY_TEXT",
    "SUBAGENT_TASK_CALL_ID",
    "TOOL_FILE_CONTENT",
    "TOOL_FILE_PATH",
    "TOOL_LS_CALL_ID",
    "TOOL_LS_PATH",
    "TOOL_TODO_ARGS",
    "approval_execute",
    "long_500_tokens",
    "multi_turn",
    "scenario_a_round_one",
    "scenario_a_round_three",
    "scenario_a_round_two",
    "simple_reply",
    "stop_stream",
    "subagent_execute",
    "subagent_task_then_reply",
    "tool_execute",
    "tool_fail",
    "tool_loop_read_only",
    "tool_loop_todo",
]
