"""golden fixtures 录制脚本（test-plan §3.3：录制来源 = 「假模型 × 真图」跑一遍 dump）。

用法（从仓库根跑，产出一次后随测试入库、不随录制者漂移）：

    uv run --package lcode-server python apps/server/test/support/record_golden.py

产出（两份都要入库，测试按行重放比对）：
- `apps/server/test/fixtures/raw_streams/<scenario>.jsonl`：真 langgraph astream 的原始
  事件序列（每行一个 JSON 对象，schema 见 `_encode_raw` / `load_stream`）；
- `apps/server/test/fixtures/golden_events/<scenario>.json`：期望契约事件序列
  （`replay_scenario` 的产物，即 translator 对该 raw 流的逐字节期望输出）。

录制口径（重要，属于 U2 spike 结论的一部分）：
- 用 `subgraphs=True` 录制 —— 子智能体事件必须带 namespace 元组才能做 AgentRef 归属，
  不开它根本拿不到归属信息；translator 同时兼容 2 元组形态（unit 测试覆盖）；
- 模型是**本文件自带的最小假流式模型**（`ScriptedStreamingChatModel`）：分块 yield
  文本 / 思考（`reasoning_content`）/ tool_call 片 / usage / `chunk_position="last"`；
  M2 任务 T4 的 `test/support/fake_llm.py` 落地后，本文件可切换为复用它（剧本语义不变）；
- 图是**与内层 deepagents 同构的最小 ReAct 环**（model → tools → model）+ 程序化调用
  子图的 task 工具替身（deepagents `subagents.py` 同构：在工具内 `sub_app.ainvoke`），
  所以嵌套命名空间形态（`parent:uuid|child:uuid`）与真装配一致；
- 工具替身复刻 deepagents 0.6.12 的正文形态：execute 成功也回 `[Command ... exit code]`
  标记（`middleware/filesystem.py:1752-1760` 实测）、错误由工具**直接返回** error
  ToolMessage、错误文案用 `Error: Execution not available.`（分类器回归锚）。

确定性：`replay_scenario` 用固定时钟（每次调用 +1ms）与固定 id 序列，golden 与重放两侧
共用同一条路径（本文件），保证逐字节可比。
"""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import PrivateAttr

from app.events.translator import EventTranslator
from lcode_contracts.events import BaseEvent

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
RAW_DIR = FIXTURES_DIR / "raw_streams"
GOLDEN_DIR = FIXTURES_DIR / "golden_events"

SESSION_ID = "3f1c9a52-6f77-4a0f-9a1e-7b2c4d5e6f70"  # 固定值：golden 与单测同源可对照
RUN_ID = "9d8e7f60-1a2b-4c3d-8e4f-5a6b7c8d9e01"
USER_INPUT = "帮我看看这个仓库，先排个计划再执行"

SCENARIO_NAMES: tuple[str, ...] = ("simple_reply", "tool_loop_todos", "subagent_task")
_MESSAGE_MARKER = "__message__"


# ---------------------------------------------------------------- 假流式模型（剧本化）


class ScriptedStreamingChatModel(BaseChatModel):
    """按剧本逐块 yield 的假流式模型（仅 fixtures 录制用；正式替身归 T4 support/fake_llm.py）。"""

    scripts: list[list[AIMessageChunk]]
    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "lcode-scripted-streaming"

    def _take_script(self) -> list[AIMessageChunk]:
        script = self.scripts[self._cursor % len(self.scripts)]
        self._cursor += 1
        return script

    async def _astream(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ):
        for chunk in self._take_script():
            yield ChatGenerationChunk(message=chunk)

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        final: AIMessageChunk | None = None
        for chunk in self._take_script():
            final = chunk if final is None else final + chunk
        assert final is not None
        return ChatResult(generations=[ChatGeneration(message=final)])


def _chunk(
    *,
    text: str | None = None,
    reasoning: str | None = None,
    tool: tuple[str, dict[str, Any], str] | None = None,  # (name, args, tool_call_id)
    usage: Any = None,  # UsageMetadata 形状的运行期 dict（含 output_token_details 嵌套），类型面按 Any
    last: bool = False,
    message_id: str = "run-msg",
) -> AIMessageChunk:
    """剧本块：文本 / 思考 / tool_call 片 / 收口（usage + chunk_position=last）。"""
    if text is not None:
        return AIMessageChunk(content=text, id=message_id)
    if reasoning is not None:
        return AIMessageChunk(
            content="", id=message_id, additional_kwargs={"reasoning_content": reasoning}
        )
    if tool is not None:
        name, args, call_id = tool
        return AIMessageChunk(
            content="",
            id=message_id,
            tool_call_chunks=[
                {
                    "name": name,
                    "args": json.dumps(args, ensure_ascii=False),
                    "id": call_id,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )
    return AIMessageChunk(
        content="",
        id=message_id,
        chunk_position="last" if last else None,
        usage_metadata=usage,
    )


# ---------------------------------------------------------------- 最小同构图


class _State(TypedDict):
    messages: Annotated[list, lambda left, right: left + right]  # noqa: E731 —— 剧本图用拼接去重即可


def _aggregate(model: BaseChatModel):
    """消费模型的流并把分片聚合成一条消息（真实 astream 的 messages/updates 双通道由此而来）。"""

    async def call_model(state: _State) -> dict[str, Any]:
        final: AIMessageChunk | None = None
        async for chunk in model.astream(list(state["messages"])):
            final = chunk if final is None else final + chunk
        assert final is not None
        return {"messages": [final]}

    return call_model


def _route(state: _State) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


def build_app(model: BaseChatModel, *, tools: tuple[Any, ...] = ()) -> Any:
    """model →(有 tool_calls)→ tools → model 的最小循环（与内层 deepagents 同构）。"""
    builder = StateGraph(_State)
    builder.add_node("agent", _aggregate(model))
    if tools:
        # handle_tool_errors=True：ToolException → error ToolMessage（deepagents 工具面同款语义）
        builder.add_node("tools", ToolNode(list(tools), handle_tool_errors=True))
        builder.add_edge("tools", "agent")
        builder.add_conditional_edges("agent", _route, {"tools": "tools", END: END})
    else:
        builder.add_edge("agent", END)
    builder.add_edge(START, "agent")
    return builder.compile()


def _build_sub_app(model: BaseChatModel) -> Any:
    """子智能体图：单节点 worker（deepagents task 的最小同构替身——程序化调用子图）。"""
    builder = StateGraph(_State)
    builder.add_node("worker", _aggregate(model))
    builder.add_edge(START, "worker")
    builder.add_edge("worker", END)
    return builder.compile()


def make_task_tool(sub_model: BaseChatModel):
    """task 工具替身：在工具内 ainvoke 子图（deepagents subagents.py 的调用形态）。"""

    @tool(description="派发子智能体（deepagents task 的最小同构替身，仅 fixtures 录制用）")
    async def task(description: str, subagent_type: str) -> str:
        sub_app = _build_sub_app(sub_model)
        result = await sub_app.ainvoke({"messages": [HumanMessage(content=description)]})
        return str(result["messages"][-1].content)

    return task


@tool(description="写入 todo 列表（langchain TodoListMiddleware 同构替身）")
def write_todos(todos: list[dict[str, str]]) -> str:
    return f"Updated todo list to {todos}"


@tool(description="执行命令（deepagents execute 同构替身：成功也回 deepagents 正文标记）")
def execute(command: str) -> str:
    return "total 0\n[Command failed with exit code 2]\n[Output was truncated due to size limits]"


@tool(description="总是失败的沙箱命令替身（deepagents 形态：工具直接返回 error ToolMessage）")
def failing_execute(command: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> ToolMessage:
    return ToolMessage(
        content="Error: Execution not available. sandbox crashed",
        name="failing_execute",
        tool_call_id=tool_call_id,
        status="error",
    )


# ---------------------------------------------------------------- 三个场景


def _scenario_simple_reply() -> Any:
    model = ScriptedStreamingChatModel(
        scripts=[
            [
                _chunk(reasoning="让我想想怎么回答", message_id="run-msg-1"),
                _chunk(text="你好，", message_id="run-msg-1"),
                _chunk(text="世界！", message_id="run-msg-1"),
                _chunk(
                    usage={
                        "input_tokens": 12,
                        "output_tokens": 9,
                        "total_tokens": 21,
                        "output_token_details": {"reasoning": 6},
                    },
                    last=True,
                    message_id="run-msg-1",
                ),
            ]
        ]
    )
    return build_app(model)


def _scenario_tool_loop_todos() -> Any:
    model = ScriptedStreamingChatModel(
        scripts=[
            [
                _chunk(
                    tool=(
                        "write_todos",
                        {
                            "todos": [
                                {"content": "读代码", "status": "in_progress"},
                                {"content": "改代码", "status": "pending"},
                            ]
                        },
                        "call-todos",
                    ),
                    message_id="run-msg-2",
                ),
                _chunk(
                    usage={"input_tokens": 30, "output_tokens": 40, "total_tokens": 70},
                    last=True,
                    message_id="run-msg-2",
                ),
            ],
            [
                _chunk(
                    tool=("execute", {"command": "ls -la"}, "call-exec"),
                    message_id="run-msg-3",
                ),
                _chunk(
                    usage={"input_tokens": 90, "output_tokens": 60, "total_tokens": 150},
                    last=True,
                    message_id="run-msg-3",
                ),
            ],
            [
                _chunk(
                    tool=("failing_execute", {"command": "make test"}, "call-fail"),
                    message_id="run-msg-4",
                ),
                _chunk(
                    usage={"input_tokens": 130, "output_tokens": 70, "total_tokens": 200},
                    last=True,
                    message_id="run-msg-4",
                ),
            ],
            [
                _chunk(text="计划执行完成", message_id="run-msg-5"),
                _chunk(
                    usage={"input_tokens": 180, "output_tokens": 82, "total_tokens": 262},
                    last=True,
                    message_id="run-msg-5",
                ),
            ],
        ]
    )
    return build_app(model, tools=(write_todos, execute, failing_execute))


def _scenario_subagent_task() -> Any:
    sub_model = ScriptedStreamingChatModel(
        scripts=[
            [
                _chunk(reasoning="子代理在侦察", message_id="run-sub-1"),
                _chunk(text="目录结构如下", message_id="run-sub-1"),
                _chunk(
                    usage={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
                    last=True,
                    message_id="run-sub-1",
                ),
            ]
        ]
    )
    model = ScriptedStreamingChatModel(
        scripts=[
            [
                _chunk(
                    tool=(
                        "task",
                        {"description": "侦察仓库结构", "subagent_type": "explorer"},
                        "call-task-1",
                    ),
                    message_id="run-msg-6",
                ),
                _chunk(
                    usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    last=True,
                    message_id="run-msg-6",
                ),
            ],
            [
                _chunk(text="侦察完成：单层目录", message_id="run-msg-7"),
                _chunk(
                    usage={"input_tokens": 60, "output_tokens": 25, "total_tokens": 85},
                    last=True,
                    message_id="run-msg-7",
                ),
            ],
        ]
    )
    return build_app(model, tools=(make_task_tool(sub_model),))


_SCENARIOS: dict[str, Any] = {
    "simple_reply": _scenario_simple_reply,
    "tool_loop_todos": _scenario_tool_loop_todos,
    "subagent_task": _scenario_subagent_task,
}


# ---------------------------------------------------------------- 原始流编码 / 解码（JSONL schema）


def _encode_raw(chunk: Any) -> dict[str, Any]:
    """astream chunk → JSONL 行：{namespace, mode, message+metadata | custom | updates}。"""
    if isinstance(chunk, (tuple, list)) and len(chunk) == 3:
        namespace, mode, payload = chunk
    else:
        namespace, mode, payload = (), chunk[0], chunk[1]
    line: dict[str, Any] = {
        "namespace": [str(part) for part in namespace],
        "mode": str(mode),
    }
    if mode == "messages":
        message, metadata = payload
        line["message"] = message_to_dict(message)
        line["metadata"] = _jsonable(metadata)
    elif mode == "custom":
        line["custom"] = _jsonable(payload)
    elif mode == "updates":
        line["updates"] = _encode_updates(payload)
    else:  # 录制期只保留翻译器关心的三种 mode；其他（如 values）不入库
        line["mode"] = "__ignored__"
    return line


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return {_MESSAGE_MARKER: message_to_dict(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _encode_updates(payload: Any) -> Any:
    return _jsonable(payload)


def _decode_raw(line: dict[str, Any]) -> Any:
    """JSONL 行 → 喂给 translator 的原始元组（与 astream 产出同构）。"""
    namespace = tuple(line.get("namespace") or [])
    mode = line["mode"]
    if mode == "messages":
        message = messages_from_dict([line["message"]])[0]
        return (namespace, mode, (message, line.get("metadata") or {}))
    if mode == "custom":
        return (namespace, mode, line["custom"])
    if mode == "updates":
        return (namespace, mode, _decode_updates(line["updates"]))
    raise ValueError(f"未知录制 mode：{mode!r}")


def _decode_updates(payload: Any) -> Any:
    if isinstance(payload, dict):
        if _MESSAGE_MARKER in payload and len(payload) == 1:
            return messages_from_dict([payload[_MESSAGE_MARKER]])[0]
        return {key: _decode_updates(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_decode_updates(item) for item in payload]
    return payload


def load_stream(path: Path) -> list[Any]:
    """读取 raw_streams/*.jsonl → 原始事件元组列表（测试与录制共用同一 schema）。"""
    return [
        _decode_raw(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def raw_path(scenario: str) -> Path:
    return RAW_DIR / f"{scenario}.jsonl"


def golden_path(scenario: str) -> Path:
    return GOLDEN_DIR / f"{scenario}.json"


# ---------------------------------------------------------------- 确定性注入 + 重放


class _DeterministicClock:
    """固定起点、每次调用 +1ms 的假时钟（duration_ms / created_at 全部剧本化）。"""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now


def make_recording_translator() -> EventTranslator:
    """录制与断言共用的确定性 translator（同一条路径，golden 才可逐字节比对）。"""
    clock = _DeterministicClock()
    counter = itertools.count(1)

    def new_id() -> str:
        return f"assistant-{next(counter):04d}"

    return EventTranslator(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        now=clock.now,
        new_id=new_id,
    )


def replay_scenario(scenario: str) -> list[BaseEvent]:
    """raw fixture → 契约事件序列（含 run_status started/finished 的完整生命周期）。"""
    translator = make_recording_translator()
    events: list[BaseEvent] = []
    started = translator.start_run()
    if started is not None:
        events.append(started)
    for raw in load_stream(raw_path(scenario)):
        events.extend(translator.translate(raw))
    events.extend(translator.finish_run("finished"))
    return events


# ---------------------------------------------------------------- 录制入口


async def record_raw(scenario: str) -> list[dict[str, Any]]:
    """真 langgraph astream 跑一遍 → JSONL 行序列（subgraphs=True，理由见模块 docstring）。"""
    app = _SCENARIOS[scenario]()
    lines: list[dict[str, Any]] = []
    async for chunk in app.astream(
        {"messages": [HumanMessage(content=USER_INPUT)]},
        stream_mode=["messages", "custom", "updates"],
        subgraphs=True,
    ):
        lines.append(_encode_raw(chunk))
    return [line for line in lines if line["mode"] != "__ignored__"]


def record_all(fixtures_dir: Path = FIXTURES_DIR) -> None:
    """录制全部场景：raw_streams/*.jsonl + golden_events/*.json（幂等覆盖）。"""
    raw_dir = fixtures_dir / "raw_streams"
    golden_dir = fixtures_dir / "golden_events"
    raw_dir.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIO_NAMES:
        raw = asyncio.run(record_raw(scenario))
        (raw_dir / f"{scenario}.jsonl").write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in raw),
            encoding="utf-8",
        )
        events = replay_scenario(scenario)
        golden = [event.model_dump(mode="json") for event in events]
        (golden_dir / f"{scenario}.json").write_text(
            json.dumps(golden, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"{scenario}: raw={len(raw)} events={len(golden)}")


if __name__ == "__main__":
    record_all()
