"""unit：事件翻译器（A3 §6.2 映射表；test-plan §2.3 AC-003-1/2、§3.3 fixtures、§5 T5）。

覆盖两条通道：
1. **手工构造的原始流**（本文件内联）：逐条映射规则（delta/finished/tool 事件族/
   AgentRef/run_status/custom 通道/P0 守卫），断言精确到字段；
2. **录制 fixtures**（`fixtures/raw_streams/*.jsonl` + `fixtures/golden_events/*.json`）：
   由 `test/support/record_golden.py` 用**真 langgraph 图 + 假流式模型**录制，重放后与
   golden 逐条比对 —— U2「AgentRef 推导 metadata 键」的回归锚（langgraph 1.2.16 实测：
   metadata 键 `checkpoint_ns` / `langgraph_checkpoint_ns`，嵌套形态 `parent:uuid|child:uuid`）。

U2 spike 实测口径（安装版 langchain-core 1.6.12 / langgraph 1.2.16）：
- astream 产出 `(mode, payload)`；配 `subgraphs=True` 时为 `(namespace_tuple, mode, payload)`；
- thinking 增量主路径 = **Anthropic thinking 块**（ADR-007）：langchain-anthropic 1.7.6
  流式把增量放在 `chunk.content`（`{"type": "thinking", "thinking": ..., "index": N}`），
  其 `content_blocks` 派生视图归一化为 `{"type": "non_standard", "value": {...}}`——
  两种构造形态在翻译器里走同一条解包分支（本文件两条都有用例）；
  ⚠️ 旧注释"ChatOpenAI 保留 reasoning_content"已被源码实测证伪（langchain-openai
  1.7.2 的 `_convert_dict_to_message` 丢弃该字段），该键仅作 legacy 兜底分支保留；
- usage 在 `chunk.usage_metadata = {input_tokens, output_tokens, total_tokens}`；
- 收口信号 `chunk.chunk_position == "last"`。
"""

import importlib.util
import itertools
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Interrupt
from pydantic import TypeAdapter, ValidationError

from app.events.translator import EventTranslator, InFlightToolCall, InterruptMappingError
from lcode_contracts.common import Usage
from lcode_contracts.errors import RunErrorObj
from lcode_contracts.events import P0_EVENT_TYPES, Event, RunStatusEvent

_UTC = UTC
SESSION_ID = "3f1c9a52-6f77-4a0f-9a1e-7b2c4d5e6f70"
RUN_ID = "9d8e7f60-1a2b-4c3d-8e4f-5a6b7c8d9e01"

_EVENT_SOURCE: Any = Event  # 运行期计算的判别联合（mypy 视作变量非类型），构造实参经 Any 中转
_ADAPTER: TypeAdapter[Any] = TypeAdapter(_EVENT_SOURCE)


# ---------------------------------------------------------------- 测试脚手架


class _Clock:
    """可推进的假时钟：`duration_ms` / `created_at` 断言来自剧本值而非真时钟（test-plan §3.6）。"""

    def __init__(self) -> None:
        self.now_at = datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC)

    def now(self) -> datetime:
        return self.now_at

    def advance(self, seconds: float) -> None:
        self.now_at += timedelta(seconds=seconds)


def _ids() -> Iterator[str]:
    """确定性 id 序列（翻译器为每条 assistant 消息生成 id，测试要能逐条断言）。"""
    counter = itertools.count(1)
    while True:
        yield f"msg-{next(counter)}"


def _translator(clock: _Clock | None = None, **kwargs: Any) -> EventTranslator:
    clock = clock or _Clock()
    ids = _ids()
    return EventTranslator(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        now=clock.now,
        new_id=lambda: next(ids),
        **kwargs,
    )


def _messages(chunk: AIMessageChunk, metadata: dict[str, Any] | None = None) -> tuple[Any, ...]:
    return ("messages", (chunk, metadata or {}))


def _updates(node: str, payload: Any) -> tuple[Any, ...]:
    return ("updates", {node: payload})


def _assert_contract(events: list[Any]) -> None:
    """出口载荷必须逐条过契约判别联合校验（AC-008-2 的测试锚，§6.5 #5）。"""
    for event in events:
        _ADAPTER.validate_python(event.model_dump(mode="python"))


def _started(tr: EventTranslator) -> Any:
    """`start_run` 的测试投影（单事件返回面按 Any 取）。"""
    return tr.start_run()


def _finished(tr: EventTranslator, *args: Any, **kwargs: Any) -> list[Any]:
    """`finish_run` 的测试投影（同 `_translated`：返回面按 Any 取）。"""
    return tr.finish_run(*args, **kwargs)


def _translated(tr: EventTranslator, raw: Any) -> list[Any]:
    """`translate` 的测试投影：返回面按 Any 取——属性断言是动态面，行为正确性由
    `_assert_contract` 的判别联合校验兜底（`translate` 的 app 注解保持精确不动）。"""
    return tr.translate(raw)


# ---------------------------------------------------------------- 消息族


def test_token_chunks_to_message_delta_with_stable_message_id() -> None:
    tr = _translator()
    first = _translated(tr, _messages(AIMessageChunk(content="你", id="c1")))
    second = _translated(tr, _messages(AIMessageChunk(content="好", id="c1")))

    assert [e.type for e in first + second] == ["message_delta", "message_delta"]
    assert [e.delta for e in first + second] == ["你", "好"]
    assert {e.kind for e in first + second} == {"text"}
    assert first[0].message_id == second[0].message_id  # 同一 assistant 消息贯穿
    assert first[0].role == "assistant"
    assert first[0].session_id == SESSION_ID and first[0].run_id == RUN_ID
    assert first[0].agent is None  # 主智能体
    _assert_contract(first + second)


def test_thinking_chunks_emit_thinking_delta() -> None:
    tr = _translator()
    thinking = _translated(
        tr, _messages(AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先想"}))
    )
    text = _translated(tr, _messages(AIMessageChunk(content="答案")))

    assert [e.kind for e in thinking + text] == ["thinking", "text"]
    assert [e.delta for e in thinking + text] == ["先想", "答案"]
    _assert_contract(thinking + text)


def test_content_block_reasoning_also_maps_to_thinking() -> None:
    """content_blocks 标准形态（{'type': 'reasoning'}）同样识别。"""
    tr = _translator()
    events = _translated(
        tr, _messages(AIMessageChunk(content=[{"type": "reasoning", "reasoning": "推理块"}]))
    )
    assert [(e.kind, e.delta) for e in events] == [("thinking", "推理块")]


def test_anthropic_thinking_content_block_emits_thinking_delta() -> None:
    """ADR-007 主路径：Anthropic thinking 块（langchain-anthropic 1.7.6 流式实测形态）

    增量位于 chunk.content（{"type": "thinking", "thinking": ..., "index": N}），
    content_blocks 派生视图把它包成 {"type": "non_standard", "value": {...}}——
    翻译器解包后取 thinking 文本，产出 message_delta(kind=thinking)。
    """
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(content=[{"type": "thinking", "thinking": "先想一步", "index": 0}])
        ),
    )
    assert [(e.kind, e.delta) for e in events] == [("thinking", "先想一步")]
    _assert_contract(events)


def test_anthropic_thinking_via_content_blocks_kwarg_also_recognized() -> None:
    """直接以 content_blocks 构造的 thinking 块（同归一化为 non_standard 包裹）同样识别。"""
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="",
                content_blocks=[{"type": "thinking", "thinking": "块内思考"}],  # type: ignore[call-arg]  # langchain_core 标注缺口：运行期接受（测试实证）
            )
        ),
    )
    assert [(e.kind, e.delta) for e in events] == [("thinking", "块内思考")]


def test_anthropic_multiple_thinking_blocks_concatenated() -> None:
    """单 chunk 多个 thinking 块：全部收集、按实现顺序拼接，不静默丢弃后续块。"""
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content=[
                    {"type": "thinking", "thinking": "先想", "index": 0},
                    {"type": "thinking", "thinking": "再想", "index": 1},
                ]
            )
        ),
    )
    assert [(e.kind, e.delta) for e in events] == [("thinking", "先想再想")]
    _assert_contract(events)


def test_anthropic_mixed_blocks_signature_only_contributes_nothing() -> None:
    """多块中夹 signature-only 空块：空块不计增量，其余块照常按序拼接。"""
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content=[
                    {"type": "thinking", "thinking": "甲", "index": 0},
                    {"type": "thinking", "thinking": "", "signature": "sig", "index": 1},
                    {"type": "thinking", "thinking": "乙", "index": 2},
                ]
            )
        ),
    )
    assert [(e.kind, e.delta) for e in events] == [("thinking", "甲乙")]
    _assert_contract(events)


def test_anthropic_signature_only_chunk_emits_nothing() -> None:
    """signature-only 收口块（thinking 为空串）不产生任何增量事件。"""
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content=[{"type": "thinking", "thinking": "", "signature": "sig-x", "index": 0}]
            )
        ),
    )
    assert events == []


def test_anthropic_thinking_then_text_share_message_id() -> None:
    """thinking 与 text 增量同属一条 assistant 消息，kind 区分，互不污染。"""
    tr = _translator()
    thinking = _translated(
        tr,
        _messages(AIMessageChunk(content=[{"type": "thinking", "thinking": "思考中", "index": 0}])),
    )
    text = _translated(
        tr, _messages(AIMessageChunk(content=[{"type": "text", "text": "答案", "index": 1}]))
    )
    assert [(e.kind, e.delta) for e in thinking + text] == [
        ("thinking", "思考中"),
        ("text", "答案"),
    ]
    assert thinking[0].message_id == text[0].message_id
    _assert_contract(thinking + text)


def test_chunk_position_last_closes_message_with_finish_reason() -> None:
    tr = _translator()
    _translated(tr, _messages(AIMessageChunk(content="hi")))
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="",
                chunk_position="last",
                response_metadata={"finish_reason": "length"},
            )
        ),
    )

    assert [e.type for e in events] == ["message_finished"]
    assert events[0].finish_reason == "length"
    _assert_contract(events)


def test_usage_includes_reasoning_tokens() -> None:
    """AC-015-1：推理 token 计入 output（网关口径 output_tokens 已含思考 token，不剥离不双计）。"""
    tr = _translator()
    closed = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="done",
                chunk_position="last",
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 9,
                    "total_tokens": 21,
                    "output_token_details": {"reasoning": 6},  # 9 中的 6 个是推理 token
                },
            )
        ),
    )
    # updates 通道的聚合 AIMessage 是同一条消息的尾巴：不再重复发事件（只留工具卡片路径）
    tail = _translated(tr, _updates("model", {"messages": [AIMessage(content="done")]}))

    assert tail == []
    finished = [e for e in closed if e.type == "message_finished"][0]
    assert finished.usage == Usage(input_tokens=12, output_tokens=9)
    assert tr.usage_totals == Usage(input_tokens=12, output_tokens=9)


# ---------------------------------------------------------------- 模型调用记账（AC-015-1 逐次口径）


def test_model_call_recorded_per_invocation_with_injected_clock() -> None:
    """单次模型调用 = 1 条记录；起止时刻/延迟来自注入时钟，不是真墙钟。"""
    clock = _Clock()
    tr = _translator(clock)
    started = clock.now()

    _translated(tr, _messages(AIMessageChunk(content="你好", id="c1")))
    clock.advance(0.25)
    _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="",
                id="c1",
                chunk_position="last",
                usage_metadata={"input_tokens": 12, "output_tokens": 9, "total_tokens": 21},
            )
        ),
    )

    records = tr.model_calls
    assert len(records) == 1
    record = records[0]
    assert record.message_id == "msg-1"
    assert record.started_at == started
    assert record.finished_at == started + timedelta(seconds=0.25)
    assert (record.input_tokens, record.output_tokens) == (12, 9)
    assert record.agent_id is None and record.role is None  # 主智能体


def test_tool_loop_records_one_row_per_model_call_with_matching_totals() -> None:
    """工具循环：每次模型调用各一条记录，token 分项正确且合计等于 usage_totals。"""
    tr = _translator()
    tool_call_message = AIMessage(
        content="",
        id="m1",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "读代码", "status": "in_progress"}]},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
        usage_metadata={"input_tokens": 30, "output_tokens": 40, "total_tokens": 70},
    )
    _translated(tr, _updates("model", {"messages": [tool_call_message]}))
    _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="Updated todo list", tool_call_id="call-1", status="success"
                    )
                ]
            },
        ),
    )
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="完成",
                        id="m2",
                        usage_metadata={
                            "input_tokens": 90,
                            "output_tokens": 60,
                            "total_tokens": 150,
                        },
                    )
                ]
            },
        ),
    )

    records = tr.model_calls
    assert len(records) == 2
    assert [(r.input_tokens, r.output_tokens) for r in records] == [(30, 40), (90, 60)]
    assert (records[0].message_id, records[1].message_id) == ("msg-1", "msg-2")
    assert tr.usage_totals == Usage(input_tokens=120, output_tokens=100)
    assert sum(r.input_tokens for r in records) == tr.usage_totals.input_tokens
    assert sum(r.output_tokens for r in records) == tr.usage_totals.output_tokens


def test_subagent_call_records_own_agent_id_and_role_without_merging() -> None:
    """子智能体记录带独立 agent_id/role，不与主智能体的行合并。"""
    tr = _translator()
    # 主智能体发 task() 的模型调用（无 usage 的空工具回合也落一条 0 token 行）
    _translated(tr, _updates("model", {"messages": [_task_call()]}))
    sub = _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="子代理在侦察",
                id="sub-1",
                chunk_position="last",
                usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
            ),
            {"checkpoint_ns": "tools:abc|worker:def"},
        ),
    )

    assert sub[0].agent is not None
    records = tr.model_calls
    assert len(records) == 2
    assert (records[0].agent_id, records[0].role) == (None, None)
    assert records[0].message_id != records[1].message_id
    assert records[1].agent_id == "worker:def"
    assert records[1].role == "explorer"  # task(args.subagent_type)
    assert (records[1].input_tokens, records[1].output_tokens) == (7, 5)


def test_model_call_without_usage_still_records_zero_token_row() -> None:
    """空工具回合（无文本/无 usage，不发 message_finished）仍必须落一条 0 token 记录。"""
    tr = _translator()
    events = _translated(tr, _updates("model", {"messages": [_write_todos_call()]}))

    assert [e.type for e in events] == ["tool_call_started"]  # 不发明空收口
    records = tr.model_calls
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (0, 0)


def test_usage_accumulates_across_messages_and_takes_running_max() -> None:
    """跨消息求和；同一消息内多块给 usage（累计方言）取分项最大值，不双计。"""
    tr = _translator()
    _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="a",
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )
        ),
    )
    _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="b",
                chunk_position="last",
                usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            )
        ),
    )
    # 第二条 assistant 消息（new_id 生成了新 message_id）
    _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="c",
                chunk_position="last",
                usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            )
        ),
    )

    assert tr.usage_totals == Usage(input_tokens=8, output_tokens=11)


def test_updates_aggregate_message_does_not_duplicate_streamed_content() -> None:
    """流式后 updates 里的聚合 AIMessage 只能收口，不得重发整段 delta。"""
    tr = _translator()
    _translated(tr, _messages(AIMessageChunk(content="he")))
    _translated(tr, _messages(AIMessageChunk(content="llo")))
    events = _translated(tr, _updates("model", {"messages": [AIMessage(content="hello")]}))

    assert [e.type for e in events] == ["message_finished"]
    assert tr.usage_totals == Usage()  # 无 usage 元数据时不假造


def test_non_streaming_message_emits_single_delta_then_finishes() -> None:
    """非流式模型（假模型剧本）：updates 直接给整段 content，补一条 delta 再收口。"""
    tr = _translator()
    events = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="整段回答",
                        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                    )
                ]
            },
        ),
    )

    assert [e.type for e in events] == ["message_delta", "message_finished"]
    assert events[0].delta == "整段回答"
    assert events[1].usage == Usage(input_tokens=2, output_tokens=3)
    _assert_contract(events)


# ---------------------------------------------------------------- 工具族


def _write_todos_call(call_id: str = "call-todos") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "读代码", "status": "in_progress"}]},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_tool_call_started_and_finished_pairing_with_todos_snapshot() -> None:
    clock = _Clock()
    tr = _translator(
        clock, risk_level_for=lambda tool, args: "low" if tool == "write_todos" else "high"
    )
    started = _translated(tr, _updates("model", {"messages": [_write_todos_call()]}))
    clock.advance(0.25)
    finished = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="Updated todo list to [...]",
                        tool_call_id="call-todos",
                        name="write_todos",
                        status="success",
                    )
                ]
            },
        ),
    )

    assert [e.type for e in started] == ["tool_call_started"]
    assert started[0].tool == "write_todos"
    assert started[0].args == {"todos": [{"content": "读代码", "status": "in_progress"}]}
    assert started[0].risk_level == "low"

    assert [e.type for e in finished] == ["tool_call_finished"]
    assert finished[0].tool_call_id == "call-todos"
    assert finished[0].status == "success"
    assert finished[0].error is None
    assert finished[0].duration_ms == 250
    assert [(t.content, t.status) for t in finished[0].todos or []] == [("读代码", "in_progress")]
    assert finished[0].exit_code is None
    _assert_contract(started + finished)


def test_deepagents_execute_markers_map_to_exit_code_and_truncation() -> None:
    """deepagents 0.6.12 把退出码/截断写进 ToolMessage 正文（filesystem.py:1752-1760）。"""
    tr = _translator()
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "ls"},
                                "id": "c-exec",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="total 0\n[Command failed with exit code 2]\n[Output was truncated due to size limits]",
                        tool_call_id="c-exec",
                        name="execute",
                        status="success",  # deepagents 口径：命令跑了就是 success
                    )
                ]
            },
        ),
    )

    finished = events[0]
    assert finished.status == "success"  # 非 0 退出码不改 status
    assert finished.exit_code == 2
    assert finished.truncated is True
    _assert_contract(events)


def test_execute_error_message_classified_with_retryable_flag() -> None:
    tr = _translator()
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "ls"},
                                "id": "c-fail",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="Error: Execution not available. sandbox crashed",
                        tool_call_id="c-fail",
                        name="execute",
                        status="error",
                    )
                ]
            },
        ),
    )

    finished = events[0]
    assert finished.status == "error"
    assert finished.error is not None
    assert finished.error.code == "sandbox_unavailable"
    assert finished.error.retryable is False  # execute 不在幂等白名单：瞬态错误也不建议自动重试


def test_idempotent_tool_transient_error_is_retryable() -> None:
    """幂等白名单内的工具（read/ls/glob/grep）遇瞬态错误才给 retryable 建议（NFR-009）。"""
    tr = _translator()
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"path": "a.py"},
                                "id": "c-read",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="Error: Execution not available. sandbox crashed",
                        tool_call_id="c-read",
                        name="read_file",
                        status="error",
                    )
                ]
            },
        ),
    )
    assert events[0].error is not None
    assert events[0].error.code == "sandbox_unavailable"
    assert events[0].error.retryable is True


def test_finished_without_started_defaults_duration_zero() -> None:
    tr = _translator()
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(content="ok", tool_call_id="ghost", name="grep", status="success")
                ]
            },
        ),
    )
    assert events[0].duration_ms == 0
    assert events[0].result_preview == "ok"


def test_result_preview_truncation_and_offload_reference() -> None:
    tr = _translator(preview_limit=10)
    long_output = "0123456789ABCDEFGHIJ"
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "cat big"},
                                "id": "c-big",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content=long_output
                        + "\nTool result too large, the result of this tool call c-big was saved in the filesystem at this path: /large_tool_results/c-big",
                        tool_call_id="c-big",
                        name="execute",
                        status="success",
                    )
                ]
            },
        ),
    )

    finished = events[0]
    assert finished.truncated is True
    assert finished.result_preview == long_output[:10]
    assert finished.result_ref == "/large_tool_results/c-big"
    _assert_contract(events)


def test_todos_snapshot_filters_unknown_status_values() -> None:
    """契约 TodoItem.status 是封闭枚举；非法条目丢弃（事件宁缺毋滥，保整条合法）。"""
    tr = _translator()
    _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_todos",
                                "args": {
                                    "todos": [
                                        {"content": "ok", "status": "completed"},
                                        {"content": "weird", "status": "doing"},
                                    ]
                                },
                                "id": "c-todos",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="Updated todo list", tool_call_id="c-todos", status="success"
                    )
                ]
            },
        ),
    )
    assert [(t.content, t.status) for t in events[0].todos or []] == [("ok", "completed")]


# ---------------------------------------------------------------- AgentRef 归属推导


def _task_call(call_id: str = "task-1", subagent: str = "explorer") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "侦察仓库结构", "subagent_type": subagent},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_agent_ref_derived_from_nested_namespace_and_task_stack() -> None:
    """U2：task() 调用栈 + 嵌套 checkpoint_ns（langgraph 1.2.16 实测 'tools:uuid|worker:uuid'）。"""
    tr = _translator()
    task_started = _translated(tr, _updates("model", {"messages": [_task_call()]}))
    assert task_started[0].agent is None  # 发起方仍是主智能体

    sub_chunk = _translated(
        tr,
        _messages(
            AIMessageChunk(content="子代理在干活"),
            {
                "langgraph_node": "worker",
                "checkpoint_ns": "tools:abc|worker:def",
                "langgraph_checkpoint_ns": "tools:abc|worker:def",
            },
        ),
    )
    assert sub_chunk[0].agent is not None
    assert sub_chunk[0].agent.role == "explorer"
    assert sub_chunk[0].agent.spawned_by_tool_call_id == "task-1"
    assert sub_chunk[0].agent.parent_agent_id is None
    assert sub_chunk[0].agent.agent_id == "worker:def"

    # 子代理的收口事件同样带归属
    sub_finished = _translated(
        tr,
        _messages(
            AIMessageChunk(content="", chunk_position="last"),
            {"checkpoint_ns": "tools:abc|worker:def"},
        ),
    )
    assert sub_finished[0].agent is not None
    assert sub_finished[0].agent.role == "explorer"

    # task 调用结束 → 后续主图事件回到 agent=null
    _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="侦察完成", tool_call_id="task-1", name="task", status="success"
                    )
                ]
            },
        ),
    )
    root = _translated(tr, _messages(AIMessageChunk(content="总结")))
    assert root[0].agent is None


def test_role_falls_back_to_namespace_node_name_without_task_frame() -> None:
    """无 task 栈时（例如同节点直接嵌套子图）：用命名空间/节点名兜底识别角色。"""
    tr = _translator()
    events = _translated(
        tr,
        _messages(
            AIMessageChunk(content="只读侦察"),
            {"langgraph_node": "explorer", "checkpoint_ns": "tools:abc|explorer:def"},
        ),
    )
    assert events[0].agent is not None
    assert events[0].agent.role == "explorer"
    assert events[0].agent.spawned_by_tool_call_id is None


def test_subagent_messages_are_tracked_independently_from_main() -> None:
    tr = _translator()
    main = _translated(tr, _messages(AIMessageChunk(content="主")))
    _translated(tr, _updates("model", {"messages": [_task_call()]}))
    sub = _translated(
        tr,
        _messages(
            AIMessageChunk(content="子", chunk_position="last"),
            {"checkpoint_ns": "tools:abc|worker:def"},
        ),
    )
    sub_finished = [e for e in sub if e.type == "message_finished"][0]

    assert main[0].message_id != sub_finished.message_id  # 两条消息各自成篇
    assert sub_finished.agent is not None


# ---------------------------------------------------------------- 运行状态族


def test_run_status_started_once_and_finished_with_usage_totals() -> None:
    tr = _translator()
    started = _started(tr)
    assert started is not None and started.type == "run_status"
    assert (started.subject, started.phase) == ("run", "started")
    assert _started(tr) is None  # 幂等：一次运行只发一次 started

    _translated(
        tr,
        _messages(
            AIMessageChunk(
                content="done",
                chunk_position="last",
                usage_metadata={"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
            )
        ),
    )
    events = _finished(tr, "finished")

    types = [e.type for e in events]
    assert types == ["run_status"]
    assert events[0].phase == "finished"
    assert events[0].usage_totals == Usage(input_tokens=4, output_tokens=6)
    _assert_contract(events)


def test_finish_run_error_requires_error_object_and_closes_open_message() -> None:
    tr = _translator()
    _translated(tr, _messages(AIMessageChunk(content="半截输出")))

    error = RunErrorObj(code="model_error", message="模型网关调用失败", retryable=True)
    events = _finished(tr, "error", error=error)

    assert [e.type for e in events] == ["message_finished", "run_status"]
    assert events[0].finish_reason == "error"  # 在飞消息以 error 收口（内容保留）
    assert events[1].error is not None and events[1].error.code == "model_error"
    assert len(tr.model_calls) == 1  # force 收口同样逐次记账
    _assert_contract(events)


def test_finish_run_without_error_object_fails_contract_validation() -> None:
    tr = _translator()
    with pytest.raises(Exception):  # noqa: B017 —— 契约 model_validator 必须拒绝
        _finished(tr, "error")


def test_finish_run_stopped_closes_message_as_cancelled() -> None:
    tr = _translator()
    _translated(tr, _messages(AIMessageChunk(content="即将被停")))
    events = _finished(tr, "stopped")

    assert [e.type for e in events] == ["message_finished", "run_status"]
    assert events[0].finish_reason == "cancelled"
    assert events[1].phase == "stopped"
    _assert_contract(events)


# ---------------------------------------------------------------- custom 通道与容错


def test_custom_passthrough_fills_envelope_for_compact_status() -> None:
    tr = _translator()
    events = _translated(
        tr,
        (
            "custom",
            {
                "type": "run_status",
                "subject": "compact",
                "phase": "finished",
                "compact": {"before_tokens": 120000, "after_tokens": 40000},
            },
        ),
    )

    assert [e.type for e in events] == ["run_status"]
    assert events[0].session_id == SESSION_ID and events[0].run_id == RUN_ID
    assert events[0].created_at is not None
    assert events[0].compact is not None and events[0].compact.before_tokens == 120000
    _assert_contract(events)


def test_custom_compact_started_and_finished_pair_roundtrips_as_contract_events() -> None:
    """tap 的 compact 载荷（started / finished+CompactInfo）经 custom 通道转契约事件。

    M3 Phase 5 的 wire 侧锚：中间件不新增事件类型，translator 补信封后必须逐条过
    `RunStatusEvent` 校验（含 finished 必须带 compact 的 validator）。
    """
    tr = _translator()
    started = _translated(
        tr, ("custom", {"type": "run_status", "subject": "compact", "phase": "started"})
    )
    finished = _translated(
        tr,
        (
            "custom",
            {
                "type": "run_status",
                "subject": "compact",
                "phase": "finished",
                "compact": {"before_tokens": 120000, "after_tokens": 40000},
            },
        ),
    )

    assert [event.type for event in (*started, *finished)] == ["run_status", "run_status"]
    assert [event.phase for event in (*started, *finished)] == ["started", "finished"]
    assert started[0].subject == "compact" and started[0].compact is None
    assert finished[0].compact is not None
    assert finished[0].compact.before_tokens == 120000
    assert finished[0].compact.after_tokens == 40000
    _assert_contract([*started, *finished])


def test_compact_subject_rejects_stopped_and_bare_finished() -> None:
    """契约反向锁定（AC-010-1）：subject=compact 无 stopped，finished 必带 compact。

    translator 的 custom 透传走构造期校验（fail loudly）——这里锁住失败面，防止
    上游误发被静默吞掉。
    """
    envelope: dict[str, Any] = {
        "seq": 0,
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "created_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=_UTC),
    }
    with pytest.raises(ValidationError):
        RunStatusEvent(subject="compact", phase="stopped", **envelope)
    with pytest.raises(ValidationError):
        # finished 必须带 compact（前后窗口占用）
        RunStatusEvent(subject="compact", phase="finished", **envelope)

    tr = _translator()
    with pytest.raises(ValidationError):
        _translated(
            tr, ("custom", {"type": "run_status", "subject": "compact", "phase": "stopped"})
        )


def test_custom_payload_without_contract_type_is_ignored() -> None:
    tr = _translator()
    assert _translated(tr, ("custom", {"note": "framework chatter"})) == []
    assert _translated(tr, ("custom", {"type": "not_a_contract_event"})) == []


def test_custom_tool_events_share_dedup_anchor_with_updates_and_messages() -> None:
    """F2：custom 通道的 tool_call_started/finished 与 messages/updates 共用去重锚。

    中间件车道可能经 custom 提前发工具卡片：无论 custom 与 updates/messages 谁先到，
    同一 tool_call_id 的 started / finished 各恰好 1 条（锚登记行为一致）。
    """
    args = {"todos": [{"content": "读代码", "status": "in_progress"}]}

    def _custom_started(call_id: str) -> tuple[Any, ...]:
        return (
            "custom",
            {
                "type": "tool_call_started",
                "tool_call_id": call_id,
                "tool": "write_todos",
                "args": args,
            },
        )

    def _custom_finished(call_id: str) -> tuple[Any, ...]:
        return (
            "custom",
            {
                "type": "tool_call_finished",
                "tool_call_id": call_id,
                "status": "success",
                "duration_ms": 5,
            },
        )

    def _tool_done(call_id: str) -> tuple[Any, ...]:
        return _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="ok", tool_call_id=call_id, name="write_todos", status="success"
                    )
                ]
            },
        )

    # 顺序一：custom 先到，updates 同 id 再报 → started/finished 各恰好 1 条
    tr = _translator()
    custom_started = _translated(tr, _custom_started("call-1"))
    updates_started = _translated(
        tr, _updates("model", {"messages": [_write_todos_call("call-1")]})
    )
    started = [e for e in custom_started + updates_started if e.type == "tool_call_started"]
    assert len(started) == 1
    assert started[0].tool == "write_todos"
    assert started[0].tool_call_id == "call-1"

    custom_finished = _translated(tr, _custom_finished("call-1"))
    updates_finished = _translated(tr, _tool_done("call-1"))
    finished = [e for e in custom_finished + updates_finished if e.type == "tool_call_finished"]
    assert len(finished) == 1
    _assert_contract(custom_started + updates_started + custom_finished + updates_finished)

    # 顺序二：updates 先到，custom 同 id 再报 → 各恰好 1 条
    tr2 = _translator()
    first_started = _translated(tr2, _updates("model", {"messages": [_write_todos_call("call-2")]}))
    second_started = _translated(tr2, _custom_started("call-2"))
    assert len([e for e in first_started + second_started if e.type == "tool_call_started"]) == 1

    first_finished = _translated(tr2, _tool_done("call-2"))
    second_finished = _translated(tr2, _custom_finished("call-2"))
    assert len([e for e in first_finished + second_finished if e.type == "tool_call_finished"]) == 1


def test_tap_custom_finish_defers_and_merges_into_tool_message_finish() -> None:
    """P0：tap 经 custom 先发的 finished 只带计时——必须寄存合并，不得抢占认领锚。

    真实接线路径（assembly 装了 LCodeEventTapMiddleware）：exit_code / result_preview /
    truncated 只在 updates 通道的 ToolMessage 收口里（deepagents 0.6.12 的 execute
    不写 artifact，正文标记是唯一来源）。custom 先到即认领会把 rich 收口整体吞掉。
    """
    clock = _Clock()
    tr = _translator(clock)

    def _custom_started() -> tuple[Any, ...]:
        return (
            "custom",
            {
                "type": "tool_call_started",
                "tool_call_id": "c-exec",
                "tool": "execute",
                "args": {"command": "ls"},
            },
        )

    def _tap_finished() -> tuple[Any, ...]:
        return (
            "custom",
            {
                "type": "tool_call_finished",
                "tool_call_id": "c-exec",
                "status": "success",
                "duration_ms": 400,
            },
        )

    custom_started = _translated(tr, _custom_started())
    assert [e.type for e in custom_started] == ["tool_call_started"]

    # updates 聚合 AIMessage 同 id 再报 started → 去重吞掉
    updates_started = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "ls"},
                                "id": "c-exec",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
        ),
    )
    assert [e for e in updates_started if e.type == "tool_call_started"] == []

    # tap 的 custom finished（只有计时）：不得立即发 finished（否则吞掉 rich 收口）
    clock.advance(0.4)
    assert _translated(tr, _tap_finished()) == []

    # updates 通道 ToolMessage：唯一携带 exit_code 标记/正文/截断的收口 → 恰好一条
    clock.advance(0.1)
    events = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="total 0\n[Command failed with exit code 2]",
                        tool_call_id="c-exec",
                        name="execute",
                        status="success",
                    )
                ]
            },
        ),
    )
    assert [e.type for e in events] == ["tool_call_finished"]
    finished = events[0]
    assert finished.duration_ms == 400  # tap 计时权威源合并进唯一收口
    assert finished.exit_code == 2
    assert finished.result_preview == "total 0\n[Command failed with exit code 2]"
    assert finished.status == "success"  # ToolMessage 是结果权威源
    _assert_contract(custom_started + events)

    # 迟到的重复 finished（若有）仍被锚吞掉：同 id 恰好一条
    assert _translated(tr, _tap_finished()) == []


def test_tap_error_finish_flushes_when_tool_message_never_arrives() -> None:
    """tap 记了错误附言但 ToolMessage 永不到（异常逃出工具节点 → run error）：
    finish_run 必须冲刷在飞工具调用，工具卡片不得悬死；附言的错误分类随之落事件。"""
    clock = _Clock()
    tr = _translator(clock)
    _translated(
        tr,
        (
            "custom",
            {
                "type": "tool_call_started",
                "tool_call_id": "c-boom",
                "tool": "execute",
                "args": {"command": "boom"},
            },
        ),
    )
    clock.advance(0.2)
    assert (
        _translated(
            tr,
            (
                "custom",
                {
                    "type": "tool_call_finished",
                    "tool_call_id": "c-boom",
                    "status": "error",
                    "duration_ms": 200,
                    "error": {
                        "code": "timeout",
                        "message": "工具执行超时",
                        "retryable": False,
                    },
                },
            ),
        )
        == []
    )

    error = RunErrorObj(code="model_error", message="模型网关调用失败", retryable=True)
    events = _finished(tr, "error", error=error)

    types = [e.type for e in events]
    assert types == ["tool_call_finished", "run_status"]
    flushed = events[0]
    assert flushed.tool_call_id == "c-boom"
    assert flushed.status == "error"
    assert flushed.duration_ms == 200
    assert flushed.error is not None and flushed.error.code == "timeout"
    _assert_contract(events)


def test_main_graph_echo_of_subgraph_message_is_not_reemitted() -> None:
    """P1：主图对 execute 子图的状态回声（namespace=()，agent=null）与内层流式消息
    不同 agent_key——回声必须被拦（否则同文气泡双发、usage 双计）。"""
    tr = _translator()
    inner_ns = ("execute:1111",)
    streamed = _translated(
        tr,
        (
            inner_ns,
            "messages",
            (
                AIMessageChunk(
                    content="子图回答",
                    id="sub-1",
                    chunk_position="last",
                    usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                ),
                {},
            ),
        ),
    )
    assert [e.type for e in streamed] == ["message_delta", "message_finished"]

    echo = _translated(
        tr,
        _updates(
            "execute",
            {
                "messages": [
                    AIMessage(
                        content="子图回答",
                        id="sub-1",
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "total_tokens": 30,
                        },
                    )
                ]
            },
        ),
    )
    assert echo == []  # 回声不再补 delta+finished
    assert tr.usage_totals == Usage(input_tokens=10, output_tokens=20)  # 不双计
    assert len(tr.model_calls) == 1  # 回声也不得产生第二行记账


def test_non_streaming_multi_turn_final_answer_not_swallowed() -> None:
    """P1：非流式模型多轮（文本+工具调用 → 工具 → 终答）——粘性「已流式」标志在
    工具回合边界清零，第二轮聚合终答不得被误判为流式尾巴整条吞掉（含 usage）。"""
    tr = _translator()
    r1 = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="先查一下",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"path": "a.py"},
                                "id": "c-read",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
                    )
                ]
            },
        ),
    )
    assert [e.type for e in r1] == ["message_delta", "message_finished", "tool_call_started"]

    tool = _translated(
        tr,
        _updates(
            "tools",
            {
                "messages": [
                    ToolMessage(
                        content="文件内容",
                        tool_call_id="c-read",
                        name="read_file",
                        status="success",
                    )
                ]
            },
        ),
    )
    assert [e.type for e in tool] == ["tool_call_finished"]

    r2 = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    AIMessage(
                        content="最终答案",
                        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
                    )
                ]
            },
        ),
    )
    assert [e.type for e in r2] == ["message_delta", "message_finished"]
    assert r2[0].delta == "最终答案"
    assert r2[1].usage == Usage(input_tokens=5, output_tokens=7)
    assert tr.usage_totals == Usage(input_tokens=10, output_tokens=14)
    _assert_contract(r1 + tool + r2)


def test_custom_structured_result_attaches_to_message_finished() -> None:
    """reviewer findings 走 message_finished.structured（A3 §6.2；Q6/Q2 定稿）。"""
    tr = _translator()
    _translated(tr, ("custom", {"structured_result": {"status": "fallback"}}))
    events = _translated(tr, _messages(AIMessageChunk(content="", chunk_position="last", id="r1")))

    finished = [e for e in events if e.type == "message_finished"][0]
    assert finished.structured is not None
    assert finished.structured.status == "fallback"
    assert finished.structured.data is None


def test_interrupt_payload_produces_no_domain_event() -> None:
    """审批族（M3）：`translate` 不构造 approval_request（approval_id 由服务层传入）。

    映射走独立入口 `describe_interrupt`（下一组用例）；本用例锁「翻译器不在没有
    审批行 id 的情况下凭空造事件」——事件与已提交审批行必须一一对应。
    """
    tr = _translator()
    payload = {"__interrupt__": (Interrupt({"action_requests": []}, id="int-1"),)}
    assert _translated(tr, ("updates", payload)) == []


# ---------------------------------------------------------------- 审批对位（M3）


def _ai_with_calls(*calls: tuple[str, str, dict[str, Any]], message_id: str = "ai-1") -> AIMessage:
    """构造一条带 tool_calls 的聚合 AIMessage（updates 通道的模型节点更新形态）。"""
    return AIMessage(
        content="",
        id=message_id,
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
            for call_id, name, args in calls
        ],
    )


def _interrupt(
    *action_requests: tuple[str, dict[str, Any]], interrupt_id: str = "int-1"
) -> Interrupt:
    return Interrupt(
        {
            "action_requests": [{"name": name, "args": args} for name, args in action_requests],
            "review_configs": [],
        },
        id=interrupt_id,
    )


def _open_call(
    tr: EventTranslator,
    call_id: str,
    name: str,
    args: dict[str, Any],
    *,
    message_id: str = "ai-1",
) -> None:
    """发一条模型节点更新，让 translator 为该调用登记在飞锚（started 已发出）。"""
    _translated(
        tr,
        _updates(
            "model",
            {"messages": [_ai_with_calls((call_id, name, args), message_id=message_id)]},
        ),
    )


def _describe(tr: EventTranslator, interrupt: Interrupt, *, namespace: tuple[str, ...] = ()) -> Any:
    return tr.describe_interrupt(namespace, interrupt)


def test_describe_interrupt_maps_action_requests_to_tool_call_ids() -> None:
    """成功对位：action_requests 按序映射到在飞 tool calls（(name,args) 精确匹配）。"""
    tr = _translator(
        risk_level_for=lambda tool, args: "high" if tool == "execute" else None,
        approval_reason=lambda tool, args: f"规则命中：{tool}",
    )
    _open_call(tr, "call-1", "execute", {"command": "rm -rf /"})

    targets = _describe(tr, _interrupt(("execute", {"command": "rm -rf /"})))

    assert len(targets) == 1
    target = targets[0]
    assert target.tool_call_id == "call-1"
    assert target.tool == "execute"
    assert target.args == {"command": "rm -rf /"}
    assert target.risk_level == "high"
    assert target.reason == "规则命中：execute"
    assert target.source_message_id == "ai-1"


def test_describe_interrupt_orders_duplicate_calls_by_appearance() -> None:
    """同名同参的重复调用按出现序一一配对（游标只前进，不重复消费）。"""
    tr = _translator()
    _open_call(tr, "call-1", "write_file", {"file_path": "/a", "content": "x"})
    _open_call(tr, "call-2", "write_file", {"file_path": "/a", "content": "x"})

    targets = _describe(
        tr,
        _interrupt(
            ("write_file", {"file_path": "/a", "content": "x"}),
            ("write_file", {"file_path": "/a", "content": "x"}),
        ),
    )
    assert [target.tool_call_id for target in targets] == ["call-1", "call-2"]


def test_describe_interrupt_tolerates_half_open_task_call() -> None:
    """父图 task 半开（started 已发、ToolMessage 等 resume 后才有）不影响对位。"""
    tr = _translator()
    _open_call(tr, "call-task", "task", {"description": "派 coder", "subagent_type": "coder"})
    _open_call(tr, "call-exec", "execute", {"command": "echo hi"})

    targets = _describe(tr, _interrupt(("execute", {"command": "echo hi"})))
    assert [target.tool_call_id for target in targets] == ["call-exec"]


def test_describe_interrupt_fails_closed_on_count_mismatch() -> None:
    """数量不符（action_requests 多于在飞匹配）→ InterruptMappingError（不自动放行）。"""
    tr = _translator()
    _open_call(tr, "call-1", "execute", {"command": "echo one"})

    with pytest.raises(InterruptMappingError, match="无法对位"):
        _describe(
            tr,
            _interrupt(
                ("execute", {"command": "echo one"}),
                ("execute", {"command": "echo two"}),
            ),
        )


def test_describe_interrupt_fails_closed_on_name_mismatch() -> None:
    """名称/参数对不上 → 抛错（run 以 error 收敛）；绝不按位置硬配。"""
    tr = _translator()
    _open_call(tr, "call-1", "execute", {"command": "echo one"})

    with pytest.raises(InterruptMappingError):
        _describe(tr, _interrupt(("write_file", {"file_path": "/a"})))


def test_describe_interrupt_rejects_malformed_payload() -> None:
    """载荷结构畸形（缺 action_requests / 非对象）→ fail-closed。"""
    tr = _translator()
    with pytest.raises(InterruptMappingError):
        _describe(tr, Interrupt({"review_configs": []}, id="int-1"))

    _open_call(tr, "call-1", "execute", {"command": "echo"})
    with pytest.raises(InterruptMappingError):
        _describe(tr, Interrupt({"action_requests": ["not-an-object"]}, id="int-2"))


def test_describe_interrupt_ignores_subgraph_duplicate_namespace() -> None:
    """子图重复条目（namespace 非空）返回 []——根层 () 是唯一映射入口（spike §1.3）。"""
    tr = _translator()
    _open_call(tr, "call-1", "execute", {"command": "echo one"})
    interrupt = _interrupt(("execute", {"command": "echo one"}))

    assert _describe(tr, interrupt, namespace=("tools:task-1",)) == []
    assert [t.tool_call_id for t in _describe(tr, interrupt)] == ["call-1"]


def test_approval_request_event_carries_contract_fields() -> None:
    """approval_request 事件：approval_id 由服务层传入，风险/理由来自注入解析器。"""
    clock = _Clock()
    tr = _translator(
        clock,
        risk_level_for=lambda tool, args: "medium",
        approval_reason=lambda tool, args: "文件写入：命中文件写审批规则（write_file）",
    )
    _open_call(tr, "call-1", "write_file", {"file_path": "/a", "content": "x"})
    target = _describe(tr, _interrupt(("write_file", {"file_path": "/a", "content": "x"})))[0]

    event = tr.approval_request_event("approval-uuid", target)

    assert event.type == "approval_request"
    assert event.approval_id == "approval-uuid"
    assert event.tool_call_id == "call-1"
    assert event.tool == "write_file"
    assert event.args == {"file_path": "/a", "content": "x"}
    assert event.risk_level == "medium"
    assert event.reason == "文件写入：命中文件写审批规则（write_file）"
    assert event.session_id == SESSION_ID and event.run_id == RUN_ID
    assert event.seq == 0  # 真实 seq 由 EventPipeline 分配
    _assert_contract([event])


def test_unknown_tool_in_interrupt_defaults_to_medium_risk() -> None:
    """未知工具出现在审批面时按 medium 兜底（不丢字段、不降级成 None）。"""
    tr = _translator()  # 未注入解析器
    _open_call(tr, "call-1", "custom_tool", {"x": 1})
    target = _describe(tr, _interrupt(("custom_tool", {"x": 1})))[0]
    assert target.risk_level == "medium"
    assert target.reason is None


# ---------------------------------------------------------------- resume 播种（M3）


def test_resumed_tool_call_ids_suppress_duplicate_started() -> None:
    """resume 播种：已 started 的挂起调用在续跑段不重发 started（tap/updates 双源）。"""
    tr = _translator(resumed_tool_call_ids=["call-1"], resumed_message_ids=["ai-1"])

    events = _translated(
        tr,
        _updates("model", {"messages": [_ai_with_calls(("call-1", "execute", {"command": "x"}))]}),
    )
    assert events == []

    # 新调用照常发 started（播种不影响本段的新调用）
    fresh = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    _ai_with_calls(("call-2", "execute", {"command": "y"}), message_id="ai-2")
                ]
            },
        ),
    )
    assert [e.type for e in fresh] == ["tool_call_started"]
    assert fresh[0].tool_call_id == "call-2"


def test_resumed_closed_tool_call_ids_suppress_history_replay() -> None:
    """resume 播种（历史重放）：已收口调用不重发 started/finished（同 run 不重复工具卡）。

    审批恢复时图从 checkpoint 续跑，execute 节点会把**整段历史**再发一遍——只播种在飞
    调用时，历史里已完成调用会二次公告。`resumed_closed_tool_call_ids` 同时写入 started
    与 finished 两道去重锚。
    """
    tr = _translator(resumed_closed_tool_call_ids=["call-done"], resumed_message_ids=["ai-old"])
    assert "call-done" in tr.closed_tool_call_ids

    # 历史 AIMessage 重放：已完成调用不重发 started
    replayed_ai = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    _ai_with_calls(("call-done", "execute", {"command": "x"}), message_id="ai-old")
                ]
            },
        ),
    )
    assert replayed_ai == []

    # 历史 ToolMessage 重放：不重发 finished
    replayed_tool = _translated(
        tr,
        ("messages", (ToolMessage(content="done", tool_call_id="call-done", name="execute"), {})),
    )
    assert replayed_tool == []

    # 本段新调用照常公告，并在收口后进入 closed 快照（供下一次挂起播种）
    fresh = _translated(
        tr,
        _updates(
            "model",
            {
                "messages": [
                    _ai_with_calls(("call-fresh", "execute", {"command": "y"}), message_id="ai-new")
                ]
            },
        ),
    )
    assert [e.type for e in fresh] == ["tool_call_started"]
    fresh_finished = _translated(
        tr,
        ("messages", (ToolMessage(content="ok", tool_call_id="call-fresh", name="execute"), {})),
    )
    assert [e.type for e in fresh_finished] == ["tool_call_finished"]
    assert "call-fresh" in tr.closed_tool_call_ids
    assert "call-fresh" not in {call.tool_call_id for call in tr.open_tool_calls}


def test_resumed_message_ids_suppress_duplicate_finish_and_model_call() -> None:
    """resume 播种：after_model 重发的挂起回合消息不重复收口/记账（防成本双计）。"""
    tr = _translator(resumed_message_ids=["ai-1"])

    events = _translated(
        tr,
        _updates(
            "after_model",
            {
                "messages": [
                    AIMessage(
                        content="",
                        id="ai-1",
                        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    )
                ]
            },
        ),
    )
    assert events == []
    assert tr.model_calls == ()
    assert tr.usage_totals == Usage()


def test_resumed_tool_calls_restore_task_stack_for_role_attribution() -> None:
    """富播种：task 调用重建 task 栈——子智能体事件仍归 coder（不是 main）。"""
    task_call = InFlightToolCall(
        tool_call_id="call-task",
        tool="task",
        args={"description": "派 coder", "subagent_type": "coder"},
        source_message_id="ai-task",
    )
    tr = _translator(resumed_tool_calls=[task_call], resumed_message_ids=["ai-task"])

    events = _translated(
        tr,
        (
            ("execute:x", "tools:y"),
            "messages",
            (
                AIMessageChunk(content="coder 在说话", id="coder-1"),
                {"checkpoint_ns": "execute:x|tools:y"},
            ),
        ),
    )

    assert [event.type for event in events] == ["message_delta"]
    agent = events[0].agent
    assert agent is not None
    assert agent.role == "coder"
    assert agent.spawned_by_tool_call_id == "call-task"
    assert agent.agent_id == "execute:x|tools:y"


def test_open_tool_calls_and_handled_message_ids_expose_seed_state() -> None:
    """播种面自省：open_tool_calls 保序、handled_message_ids 含种子与已收口消息。"""
    task_call = InFlightToolCall("call-task", "task", {"subagent_type": "coder"})
    tr = _translator(resumed_tool_calls=[task_call], resumed_message_ids=["ai-task"])
    assert [call.tool_call_id for call in tr.open_tool_calls] == ["call-task"]
    assert tr.open_tool_calls[0].tool == "task"
    assert tr.handled_message_ids == ("ai-task",)


def test_three_tuple_namespace_form_is_supported() -> None:
    """astream(subgraphs=True) 形态：(namespace_tuple, mode, payload)——元组非空即子作用域。"""
    tr = _translator()
    events = _translated(
        tr,
        (
            ("tools:abc",),
            "messages",
            (AIMessageChunk(content="hi"), {"checkpoint_ns": "tools:abc"}),
        ),
    )
    assert [e.type for e in events] == ["message_delta"]
    assert events[0].agent is not None
    assert events[0].agent.agent_id == "tools:abc"  # 元组本身就是子作用域路径


def test_unknown_mode_and_malformed_payload_are_ignored() -> None:
    tr = _translator()
    assert _translated(tr, ("values", {"messages": []})) == []
    assert _translated(tr, ("messages", (AIMessageChunk(content="x"),))) == []
    assert _translated(tr, {"unexpected": "shape"}) == []


def test_translator_never_emits_browser_action() -> None:
    tr = _translator()
    produced = []
    produced += _translated(tr, _messages(AIMessageChunk(content="x", chunk_position="last")))
    produced += _translated(tr, _updates("model", {"messages": [_task_call()]}))
    produced += _translated(
        tr, ("custom", {"type": "run_status", "subject": "run", "phase": "started"})
    )
    produced += _finished(tr, "stopped")

    assert all(e.type in P0_EVENT_TYPES for e in produced)
    _assert_contract(produced)


# ---------------------------------------------------------------- 录制 fixtures（golden）

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_RAW_DIR = _FIXTURES / "raw_streams"
_GOLDEN_DIR = _FIXTURES / "golden_events"
_SUPPORT = Path(__file__).resolve().parents[1] / "support" / "record_golden.py"

REQUIRED_SCENARIOS = {"simple_reply", "tool_loop_todos", "subagent_task"}


def _load_support() -> Any:
    spec = importlib.util.spec_from_file_location("lcode_record_golden", _SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenarios() -> list[str]:
    return sorted(p.stem for p in _RAW_DIR.glob("*.jsonl")) if _RAW_DIR.is_dir() else []


def test_fixture_set_is_complete() -> None:
    """fixtures 是交付物的一部分：缺场景直接红（test-plan §3.3 三层分账的后端 unit 层）。"""
    assert _SUPPORT.is_file(), "缺少 test/support/record_golden.py（录制脚本）"
    assert set(_scenarios()) >= REQUIRED_SCENARIOS, f"缺少场景 fixture：{_scenarios()}"


@pytest.mark.parametrize("scenario", _scenarios() or ["__missing__"])
def test_golden_replay_matches_recorded_events(scenario: str) -> None:
    assert scenario != "__missing__", "fixtures/raw_streams/ 为空：先跑 record_golden.py"
    support = _load_support()

    events = support.replay_scenario(scenario)
    golden = json.loads(support.golden_path(scenario).read_text(encoding="utf-8"))
    produced = [event.model_dump(mode="json") for event in events]

    _assert_contract(events)
    assert produced == golden
    assert all(event.type in P0_EVENT_TYPES for event in events)
