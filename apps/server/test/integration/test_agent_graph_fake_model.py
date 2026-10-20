"""Agent 运行时 integration：真图 + 假模型 + InMemorySaver + FakeSandbox（T4；A3 §9.2）。

覆盖（本车道验收集）：
- happy path：entry→router→execute 全链，run_status(started) 经 custom 流可见（AC-001-1 图侧）；
- 多轮上下文：同一 thread 两次 run，第二轮 prompt 含第一轮历史（AC-001-3 / AC-004-2）；
- 工具循环：write_todos + write_file 真执行、tap 计时/分类 custom 事件（AC-003-2 图侧）；
- usage：run 级 usage 聚合口径（AC-015-1 图侧，RunOutcome.usage = model_calls 唯一口径源）；
- stop：run 中途 request_stop → 终态 stopped（AC-001-2 的运行时收敛侧）。

替身边界（A3 §9.2）：LLM=ScriptedChatModel；沙箱=FakeSandbox（真 tmp + 真子进程）；
checkpointer=InMemorySaver（REQ-004 约束④）；不触 DB（本文件不标 requires_db）。
translator+emitter 的接线在 services/run_service.py（流式接口车道）——本文件只断言
RunExecutor 交给 EventSink 的**原始流事件**形态。
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage, UsageMetadata
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.models_registry import SessionModelCfg
from app.agent.runtime import RunExecutor, RunOutcome
from lcode_contracts.common import Usage
from test.support import scripts
from test.support.fake_llm import ScriptedChatModel, usage_of
from test.support.fake_sandbox import FakeSandbox

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ 测试装置


class RawEventCollector:
    """EventSink 替身：收集 RunExecutor 转发的原始 astream 事件。"""

    def __init__(self) -> None:
        self.events: list[object] = []
        self._first_seen: asyncio.Event | None = None
        self._first_content: asyncio.Event | None = None

    async def on_raw_event(self, raw: object) -> None:
        self.events.append(raw)
        if self._first_seen is not None:
            self._first_seen.set()
        # 首个非空文本增量（stop 测试的"流已真正开始"信号——首个事件是 entry 的
        # custom run_status，不代表模型输出已开始）
        if self._first_content is not None:
            parts = raw if isinstance(raw, tuple) else ()
            payload = parts[2] if len(parts) == 3 else (parts[1] if len(parts) == 2 else None)
            if (
                isinstance(payload, tuple)
                and payload
                and type(payload[0]).__name__ == "AIMessageChunk"
                and payload[0].content
            ):
                self._first_content.set()

    def by_mode(self, mode: str) -> list[tuple]:
        out: list[tuple] = []
        for raw in self.events:
            parts = raw if isinstance(raw, tuple) else ()
            if len(parts) == 3 and parts[1] == mode:
                out.append((parts[0], parts[2]))
            elif len(parts) == 2 and parts[0] == mode:
                out.append(((), parts[1]))
        return out

    def message_chunks(self) -> list[tuple]:
        """messages 通道展平：[(namespace, chunk, metadata), ...]。"""
        out = []
        for ns, payload in self.by_mode("messages"):
            if isinstance(payload, tuple) and len(payload) == 2:
                chunk, metadata = payload
                out.append((ns, chunk, metadata))
        return out

    def custom_payloads(self) -> list[dict]:
        payloads = []
        for _, payload in self.by_mode("custom"):
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads


@pytest.fixture
def sandbox(tmp_path: Path) -> FakeSandbox:
    client = FakeSandbox(root=tmp_path / "sbx", session_id="sess-int")
    client.create()
    return client


@pytest.fixture
def saver() -> InMemorySaver:
    return InMemorySaver()


def make_model_factory(model: ScriptedChatModel) -> Callable[[str], BaseChatModel]:
    """按角色返回同一假模型的工厂（RunExecutor 每次 run 重建 agent，模型实例共享）。"""

    def factory(role: str) -> BaseChatModel:
        model.role = role
        return model

    return factory


async def drive(
    *,
    model: ScriptedChatModel,
    sandbox: FakeSandbox,
    saver: InMemorySaver,
    text: str,
    session_id: str = "sess-int",
    run_id: str = "run-1",
) -> tuple[RunOutcome, RawEventCollector]:
    collector = RawEventCollector()
    executor = RunExecutor(
        session_id=session_id,
        run_id=run_id,
        sink=collector,
    )
    outcome = await executor.run(
        input_messages=[HumanMessage(content=text)],
        session_cfg=SessionModelCfg(),
        backend=sandbox,
        checkpointer=saver,
        model_factory=make_model_factory(model),
    )
    return outcome, collector


# ------------------------------------------------------------------ 用例


async def test_entry_to_execute_happy_path(sandbox: FakeSandbox, saver: InMemorySaver) -> None:
    """发一条消息 → entry 发 started → execute 出回复 → 终态 finished。"""
    model = ScriptedChatModel(steps=scripts.simple_reply())
    outcome, collector = await drive(
        model=model, sandbox=sandbox, saver=saver, text=scripts.SIMPLE_USER_TEXT
    )

    assert outcome.status == "finished"
    assert outcome.error is None

    customs = collector.custom_payloads()
    started = [p for p in customs if p.get("type") == "run_status" and p.get("phase") == "started"]
    assert started, "entry 节点必须经 get_stream_writer 发 run_status(started)（custom 流）"

    # messages 流：文本增量可见（AC-003-1 图侧）
    text_seen = "".join(
        str(chunk.content)
        for _, chunk, _meta in collector.message_chunks()
        if type(chunk).__name__ == "AIMessageChunk" and chunk.content
    )
    assert scripts.SIMPLE_REPLY_TEXT in text_seen

    # 子图命名空间出现（execute 的内层事件带 namespace，U2 口径）
    msg_namespaces = [ns for ns, _chunk, _meta in collector.message_chunks()]
    assert any(ns for ns in msg_namespaces), "内层 deepagents 事件必须带 subgraph namespace"

    # 模型确实被调用了一次
    assert model.invocations >= 1


async def test_multi_turn_context_carried(sandbox: FakeSandbox, saver: InMemorySaver) -> None:
    """同一 thread 两次 run：第二轮模型 prompt 包含第一轮历史（AC-001-3 / AC-004-2）。"""
    model = ScriptedChatModel(steps=scripts.multi_turn())

    first, _ = await drive(
        model=model,
        sandbox=sandbox,
        saver=saver,
        text=scripts.SIMPLE_USER_TEXT,
        run_id="run-1",
    )
    assert first.status == "finished"
    assert (first.usage.input_tokens, first.usage.output_tokens) == (10, 6)
    first_prompts = len(model.prompts)

    second, _ = await drive(
        model=model,
        sandbox=sandbox,
        saver=saver,
        text="我们刚才聊了什么？",
        run_id="run-2",
    )
    assert second.status == "finished"
    assert (second.usage.input_tokens, second.usage.output_tokens) == (25, 9)

    second_round_prompts = model.prompts[first_prompts:]
    assert second_round_prompts, "第二轮必须有模型调用"
    flat = [m for prompt in second_round_prompts for m in prompt]
    contents = [str(m.content) for m in flat]
    assert scripts.SIMPLE_USER_TEXT in contents, "第二轮 prompt 必须含第一轮用户消息"
    assert scripts.MULTI_TURN_FIRST_REPLY in contents, "第二轮 prompt 必须含第一轮回复"


async def test_tool_loop_executes_tools_and_emits_tap_events(
    sandbox: FakeSandbox, saver: InMemorySaver
) -> None:
    """工具循环：write_todos + write_file 真执行；tap 发计时/状态 custom 流（AC-003-2 图侧）。"""
    model = ScriptedChatModel(steps=scripts.tool_loop_todo())
    outcome, collector = await drive(
        model=model, sandbox=sandbox, saver=saver, text="建个 todo 并写个 hello 文件"
    )

    assert outcome.status == "finished"
    # write_file 真落盘（FakeSandbox 真子进程链路）
    written = sandbox.root / scripts.TOOL_FILE_PATH.lstrip("/")
    assert written.read_text(encoding="utf-8") == scripts.TOOL_FILE_CONTENT

    customs = collector.custom_payloads()
    tap_started = [p for p in customs if p.get("type") == "tool_call_started"]
    tap_finished = [p for p in customs if p.get("type") == "tool_call_finished"]
    tools_started = {p.get("tool") for p in tap_started}
    assert {"write_todos", "write_file"} <= tools_started

    finished_by_id = {p.get("tool_call_id"): p for p in tap_finished}
    assert set(finished_by_id) >= {"call-todo-1", "call-file-1"}
    for call_id in ("call-todo-1", "call-file-1"):
        payload = finished_by_id[call_id]
        assert payload["status"] == "success"
        assert payload["duration_ms"] >= 0, "tap 必须计时"

    # todos 快照随 write_todos 的 finished 携带（REQ-005 面板数据源）
    todo_payload = finished_by_id["call-todo-1"]
    assert todo_payload.get("todos") == scripts.TOOL_TODO_ARGS["todos"]


def _usage_chunk(
    message_id: str,
    input_tokens: int,
    output_tokens: int,
    position: Literal["last"] | None = None,
) -> AIMessageChunk:
    """usage 观察面的合成 chunk（chunk_position 模拟 langgraph 的每消息末块标记）。"""
    return AIMessageChunk(
        content="",
        id=message_id,
        usage_metadata=cast(UsageMetadata, usage_of(input_tokens, output_tokens)),
        chunk_position=position,
    )


def test_run_usage_incremental_dialect_sums_across_messages() -> None:
    """F3 增量方言锁：每消息一块、各自给用量 → run 级按消息求和 (2,1)+(3,6)=(5,7)。"""
    executor = RunExecutor(session_id="sess-int", run_id="run-usage", sink=RawEventCollector())
    usage = Usage()
    executor._observe(("", "messages", (_usage_chunk("m1", 2, 1, position="last"), {})), usage)
    executor._observe(("", "messages", (_usage_chunk("m2", 3, 6, position="last"), {})), usage)
    assert (usage.input_tokens, usage.output_tokens) == (5, 7)


def test_run_usage_cumulative_dialect_takes_max_per_message() -> None:
    """F3 累计方言：同一消息多块给累计量 → 分项取最大，不再逐块累加双计 (5,7)。"""
    executor = RunExecutor(session_id="sess-int", run_id="run-usage", sink=RawEventCollector())
    usage = Usage()
    executor._observe(("", "messages", (_usage_chunk("m1", 5, 2), {})), usage)
    executor._observe(("", "messages", (_usage_chunk("m1", 5, 7, position="last"), {})), usage)
    assert (usage.input_tokens, usage.output_tokens) == (5, 7)


async def test_run_usage_aggregates_scripted_usage_for_model_calls(
    sandbox: FakeSandbox, saver: InMemorySaver
) -> None:
    """AC-015-1 图侧锁：run 终态 usage（model_calls 的唯一口径源）= 剧本收口步用量。

    F4：on_usage 回调已删除（生产无消费者——run_service 经 RunOutcome.usage 记
    model_calls），本用例按终态面锁定同一口径（剧本只给收口步带 usage_metadata：
    前两步工具调用无用量）。
    """
    model = ScriptedChatModel(steps=scripts.tool_loop_todo())
    outcome, _ = await drive(
        model=model,
        sandbox=sandbox,
        saver=saver,
        text="跑一遍工具循环",
    )

    assert outcome.status == "finished"
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (40, 15)


async def test_stop_cancels_stream(sandbox: FakeSandbox, saver: InMemorySaver) -> None:
    """stop：流中途 request_stop → 消费循环退出、终态 stopped（AC-001-2 收敛侧）。"""
    model = ScriptedChatModel(steps=scripts.stop_stream(), chunk_size=8, delay_seconds=0.05)
    collector = RawEventCollector()
    executor = RunExecutor(session_id="sess-int", run_id="run-stop", sink=collector)

    task = executor.start(
        input_messages=[HumanMessage(content="开始长输出")],
        session_cfg=SessionModelCfg(),
        backend=sandbox,
        checkpointer=saver,
        model_factory=make_model_factory(model),
    )

    # 等首个文本增量到达（流已真正开始），再请求停止
    collector._first_content = asyncio.Event()
    await asyncio.wait_for(collector._first_content.wait(), timeout=5)
    executor.request_stop()

    outcome = await asyncio.wait_for(task, timeout=5)

    assert isinstance(outcome, RunOutcome)
    assert outcome.status == "stopped"
    assert outcome.error is None
    # 已产出的增量已进 sink（停止保留已生成内容的运行时侧语义）
    streamed = "".join(
        str(chunk.content)
        for _, chunk, _meta in collector.message_chunks()
        if type(chunk).__name__ == "AIMessageChunk" and chunk.content
    )
    assert streamed, "stop 前已生成的增量必须已转发给 sink"
