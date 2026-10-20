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


def long_500_tokens() -> list[AIMessage]:
    """LONG_500_TOKENS：一条 ≥500 token 的长回复（流式分块由模型侧 chunk_size 决定）。"""
    return [
        AIMessage(
            content=LONG_500_TEXT,
            usage_metadata=usage_of(input_tokens=120, output_tokens=LONG_TOKEN_COUNT),
        )
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
    "EXEC_FAIL_COMMAND",
    "EXEC_STREAM_COMMAND",
    "EXEC_STREAM_LINES",
    "LONG_500_TEXT",
    "LONG_TOKEN_COUNT",
    "MULTI_TURN_FIRST_REPLY",
    "MULTI_TURN_SECOND_REPLY",
    "SIMPLE_REPLY_TEXT",
    "SIMPLE_USER_TEXT",
    "STOP_STREAM_TEXT",
    "TOOL_FILE_CONTENT",
    "TOOL_FILE_PATH",
    "TOOL_TODO_ARGS",
    "long_500_tokens",
    "multi_turn",
    "simple_reply",
    "stop_stream",
    "tool_execute",
    "tool_fail",
    "tool_loop_todo",
]
