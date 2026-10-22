"""unit：上下文压缩事件检测（M3 Phase 5 / REQ-010 AC-010-1）。

覆盖 `LCodeEventTapMiddleware.awrap_model_call` 的两条检测路径与全部降级面：

1. **结果事件路径**（m3-spikes §3 主路径；tap 在摘要中间件外侧）：构造带
   `_summarization_event` 的 `ExtendedModelResponse` → started+finished 成对、
   `CompactInfo` 数值按 `count_tokens_approximately` 口径；无事件 → 零事件；
   同 cutoff → 只发一次；畸形 → 不抛、不发、留 WARNING；
2. **入口窗口路径**（安装版栈序下实际生效的路径）：外层摘要改写的 effective 窗口
   （`[summary, *preserved]`）与 state 重建窗口比对 → 判定压缩并精确重建 before/after；
   `truncate_args` 式「同长度、新对象、同 id」调用不得误判；
3. **等价真实触发**：用安装版 deepagents `SummarizationMiddleware`（假摘要模型 + 内存
   backend）真实产生一次压缩，验证 before>after>0 且下一次调用（state 已带事件）不重复。

隔离纪律：writer 经 `app.agent.middleware.get_stream_writer` 猴补捕获；WARNING 经
`_compact_warning` 猴补收集（不解析日志输出）。全文件零网络、零 DB。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

import app.agent.middleware as middleware_module
from app.agent.middleware import SUMMARIZATION_EVENT_KEY, LCodeEventTapMiddleware

_MODEL = FakeListChatModel(responses=["ok"])
_SUMMARY = HumanMessage("compacted summary of the earlier conversation", id="summary-1")


def _request(
    messages: Sequence[Any],
    *,
    state: Mapping[str, Any] | None = None,
) -> ModelRequest[Any]:
    """ModelRequest 图外直构造：state 缺省与 messages 同源（无压缩的普通调用形态）。"""
    resolved_state = state if state is not None else {"messages": list(messages)}
    return ModelRequest(
        model=_MODEL,
        messages=list(messages),
        system_message=SystemMessage("sys"),
        tools=[],
        state=cast(Any, resolved_state),  # AgentState TypedDict 不认私有键（运行期可见）
        runtime=None,
    )


def _event(cutoff_index: int, summary: BaseMessage = _SUMMARY) -> dict[str, Any]:
    return {"cutoff_index": cutoff_index, "summary_message": summary, "file_path": None}


def _rich_response(event: dict[str, Any] | None) -> ExtendedModelResponse[Any]:
    """等价触发：外层摘要中间件返回的 ExtendedModelResponse（带/不带事件）。"""
    update = {} if event is None else {SUMMARIZATION_EVENT_KEY: event}
    return ExtendedModelResponse(
        model_response=ModelResponse(result=[AIMessage("ok")]),
        command=Command(update=update),
    )


async def _plain_handler(request: ModelRequest[Any]) -> ModelResponse[Any]:
    return ModelResponse(result=[AIMessage("ok")])


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """捕获 custom 流载荷（与 test_sandbox_event_wiring 的 tap 写法同一接缝）。"""
    collected: list[dict[str, Any]] = []
    monkeypatch.setattr(middleware_module, "get_stream_writer", lambda: collected.append)
    return collected


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """收集降级 WARNING 文案（不解析 loguru 输出）。"""
    collected: list[str] = []

    def _collect(message: str, exc: Exception | None = None) -> None:
        collected.append(message)

    monkeypatch.setattr(middleware_module, "_compact_warning", _collect)
    return collected


def _phases(payloads: list[dict[str, Any]]) -> list[str]:
    return [str(payload.get("phase")) for payload in payloads]


def _compact(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    finished = payloads[-1]
    compact = finished["compact"]
    assert isinstance(compact, dict)
    return compact


# ---------------------------------------------------------------- 结果事件路径


async def test_plain_model_call_emits_nothing(writers: list[dict[str, Any]]) -> None:
    """普通调用零副作用：不写 custom 流（现有事件顺序不变）。"""
    tap = LCodeEventTapMiddleware()
    request = _request([HumanMessage("hi", id="m1")])

    await tap.awrap_model_call(request, _plain_handler)

    assert writers == []


async def test_result_event_emits_started_then_finished_with_compact_info(
    writers: list[dict[str, Any]],
) -> None:
    """带 `_summarization_event` 的响应 → started→finished，数值按契约口径。"""
    tap = LCodeEventTapMiddleware()
    messages = [HumanMessage("x" * 400, id="m1"), HumanMessage("y" * 400, id="m2")]
    request = _request(messages)

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(1))

    await tap.awrap_model_call(request, handler)

    assert [payload["type"] for payload in writers] == ["run_status", "run_status"]
    assert [payload["subject"] for payload in writers] == ["compact", "compact"]
    assert _phases(writers) == ["started", "finished"]
    assert "compact" not in writers[0]  # started 不带 compact（契约允许且不预支 finished 数据）
    expected_after = count_tokens_approximately([_SUMMARY, *messages[1:]])
    assert _compact(writers) == {
        "before_tokens": count_tokens_approximately(messages),
        "after_tokens": expected_after,
    }


async def test_same_cutoff_is_emitted_only_once(writers: list[dict[str, Any]]) -> None:
    """同一压缩（同 cutoff_index）只发一对 started+finished，不重复 finished。"""
    tap = LCodeEventTapMiddleware()
    messages = [HumanMessage("x" * 400, id="m1"), HumanMessage("y" * 400, id="m2")]
    request = _request(messages)

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(1))

    await tap.awrap_model_call(request, handler)
    first = list(writers)
    await tap.awrap_model_call(request, handler)

    assert len(first) == 2
    assert writers == first  # 第二次同 cutoff：零新增


async def test_new_cutoff_after_previous_one_is_emitted(
    writers: list[dict[str, Any]],
) -> None:
    """链式摘要 cutoff 递增 → 新压缩照常发（去重键不是「只发一次」）。"""
    tap = LCodeEventTapMiddleware()

    async def first(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(1))

    async def second(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(2))

    messages = [HumanMessage("x" * 400, id="m1"), HumanMessage("y" * 400, id="m2")]
    await tap.awrap_model_call(_request(messages), first)
    await tap.awrap_model_call(_request(messages), second)

    assert _phases(writers) == ["started", "finished", "started", "finished"]


@pytest.mark.parametrize(
    "event",
    [
        {"cutoff_index": 0, "summary_message": _SUMMARY, "file_path": None},  # 未压缩的早退形态
        {"cutoff_index": -1, "summary_message": _SUMMARY, "file_path": None},
        {"cutoff_index": "3", "summary_message": _SUMMARY, "file_path": None},
        {"cutoff_index": True, "summary_message": _SUMMARY, "file_path": None},
        {"summary_message": _SUMMARY, "file_path": None},
        {"cutoff_index": 1, "file_path": None},
    ],
)
async def test_malformed_result_event_degrades_without_raising(
    event: dict[str, Any], writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """字段畸形 → 不抛、不发事件、留 WARNING（按「无 compact 事件」降级）。"""
    tap = LCodeEventTapMiddleware()
    messages = [HumanMessage("x" * 200, id="m1"), HumanMessage("y" * 200, id="m2")]

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(event)

    result = await tap.awrap_model_call(_request(messages), handler)

    assert isinstance(result, ExtendedModelResponse)
    assert writers == []
    assert warnings  # 至少一条降级告警（cutoff<=0 与缺字段都给）


async def test_result_event_cutoff_out_of_range_degrades(
    writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """cutoff 超出请求消息数 → 不切片、不发事件（不猜）。"""
    tap = LCodeEventTapMiddleware()
    messages = [HumanMessage("x", id="m1")]

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(9))

    await tap.awrap_model_call(_request(messages), handler)

    assert writers == []
    assert any("超出请求消息数" in message for message in warnings)


async def test_non_mapping_result_event_degrades(
    writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """`_summarization_event` 不是 mapping → 告警降级，不当 dict 硬解。"""
    tap = LCodeEventTapMiddleware()

    async def handler(_: ModelRequest[Any]) -> Any:
        return ExtendedModelResponse(
            model_response=ModelResponse(result=[]),
            command=Command(update={SUMMARIZATION_EVENT_KEY: "not-a-dict"}),
        )

    await tap.awrap_model_call(_request([HumanMessage("x", id="m1")]), handler)

    assert writers == []
    assert any("形状非法" in message for message in warnings)


async def test_handler_exception_propagates_without_emitting(
    writers: list[dict[str, Any]],
) -> None:
    """模型调用异常原样上抛（run 的既有错误路径不变），检测面不发任何事件。"""
    tap = LCodeEventTapMiddleware()

    async def handler(_: ModelRequest[Any]) -> ModelResponse[Any]:
        raise RuntimeError("model boom")

    with pytest.raises(RuntimeError):
        await tap.awrap_model_call(_request([HumanMessage("x", id="m1")]), handler)

    assert writers == []


async def test_emit_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """custom 流不可用（get_stream_writer 抛）→ 只落真实 WARNING，模型响应照常返回。

    本用例**不**猴补 `_compact_warning`：走真实 `BusinessLog` 出口（降级日志形态本身
    也被覆盖，断言只要求不抛/响应不变）。
    """

    def _boom() -> Any:
        raise RuntimeError("no stream writer")

    monkeypatch.setattr(middleware_module, "get_stream_writer", _boom)
    tap = LCodeEventTapMiddleware()
    messages = [HumanMessage("x" * 200, id="m1"), HumanMessage("y" * 200, id="m2")]

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return _rich_response(_event(1))

    result = await tap.awrap_model_call(_request(messages), handler)

    assert isinstance(result, ExtendedModelResponse)


# ---------------------------------------------------------------- 入口窗口路径（安装版栈序）


async def test_in_call_compaction_detected_from_effective_window(
    writers: list[dict[str, Any]],
) -> None:
    """外层摘要已把 request.messages 换成 `[summary, *preserved]` → 入口比对判定压缩。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage(f"old-{index}" * 100, id=f"m{index}") for index in range(4)]
    compacted = [_SUMMARY, raw[-1]]

    await tap.awrap_model_call(_request(compacted, state={"messages": raw}), _plain_handler)

    assert _phases(writers) == ["started", "finished"]
    assert _compact(writers) == {
        "before_tokens": count_tokens_approximately(raw),
        "after_tokens": count_tokens_approximately(compacted),
    }


async def test_in_call_chained_compaction_uses_state_absolute_cutoff(
    writers: list[dict[str, Any]],
) -> None:
    """链式摘要：state cutoff = prior + effective_cutoff - 1（与 _compute_state_cutoff 一致）。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage(f"old-{index}" * 100, id=f"m{index}") for index in range(5)]
    prior = HumanMessage("prior summary", id="summary-0")
    prior_event = _event(2, summary=prior)
    effective_before = [prior, *raw[2:]]  # 上一个事件重建的窗口（4 条）
    new_summary = HumanMessage("new summary", id="summary-1")
    compacted = [new_summary, *effective_before[2:]]  # eff_cutoff = 2

    await tap.awrap_model_call(
        _request(compacted, state={"messages": raw, SUMMARIZATION_EVENT_KEY: prior_event}),
        _plain_handler,
    )

    assert _phases(writers) == ["started", "finished"]
    assert _compact(writers) == {
        "before_tokens": count_tokens_approximately(effective_before),
        "after_tokens": count_tokens_approximately(compacted),
    }
    # 下一次调用（state 已带新事件、窗口首条即该 summary）不重复发
    new_event = _event(3, summary=new_summary)
    emitted = list(writers)
    await tap.awrap_model_call(
        _request(
            [new_summary, *raw[3:]], state={"messages": raw, SUMMARIZATION_EVENT_KEY: new_event}
        ),
        _plain_handler,
    )
    assert writers == emitted


async def test_truncate_only_call_is_not_compaction(writers: list[dict[str, Any]]) -> None:
    """纯参数截断：同长度、新对象、保留 id → 不是压缩（不得发事件）。"""
    tap = LCodeEventTapMiddleware()
    raw = [AIMessage(content="use tool", id="a1"), HumanMessage("go", id="m2")]
    truncated = [raw[0].model_copy(), raw[1]]  # model_copy 保留 id（deepagents 截断同款）

    await tap.awrap_model_call(_request(truncated, state={"messages": raw}), _plain_handler)

    assert writers == []


async def test_malformed_state_event_degrades_without_raising(
    writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """state 事件畸形 → 不抛、不发、留 WARNING。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage("a" * 200, id="m1"), HumanMessage("b" * 200, id="m2")]

    await tap.awrap_model_call(
        _request(
            [_SUMMARY], state={"messages": raw, SUMMARIZATION_EVENT_KEY: {"cutoff_index": "x"}}
        ),
        _plain_handler,
    )

    assert writers == []
    assert warnings


async def test_state_without_messages_degrades(writers: list[dict[str, Any]]) -> None:
    """state 不可读（无 messages）→ 不做入口判定（不猜）。"""
    tap = LCodeEventTapMiddleware()

    await tap.awrap_model_call(_request([_SUMMARY], state={}), _plain_handler)

    assert writers == []


async def test_state_messages_with_non_message_entries_degrades(
    writers: list[dict[str, Any]],
) -> None:
    """state 消息列表混入非消息对象 → 整份 state 弃用（不裁剪下标，避免 cutoff 错位）。"""
    tap = LCodeEventTapMiddleware()
    raw: list[Any] = [HumanMessage("a" * 200, id="m1"), "not-a-message"]

    await tap.awrap_model_call(
        _request([_SUMMARY], state={"messages": raw}),
        _plain_handler,
    )

    assert writers == []


async def test_state_event_not_mapping_degrades(
    writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """`_summarization_event` 非 mapping（state 形态脏）→ 告警降级，不发事件。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage("a" * 200, id="m1"), HumanMessage("b" * 200, id="m2")]

    await tap.awrap_model_call(
        _request([_SUMMARY], state={"messages": raw, SUMMARIZATION_EVENT_KEY: "bogus"}),
        _plain_handler,
    )

    assert writers == []
    assert any("形状非法" in message for message in warnings)


async def test_prior_event_without_state_messages_degrades(
    writers: list[dict[str, Any]],
) -> None:
    """有上一个事件但 state 无 messages → 无法重建窗口，不猜（零事件）。"""
    tap = LCodeEventTapMiddleware()
    request = _request(
        [HumanMessage("new summary", id="summary-2")],
        state={SUMMARIZATION_EVENT_KEY: _event(2)},
    )

    await tap.awrap_model_call(request, _plain_handler)

    assert writers == []


async def test_prior_event_cutoff_beyond_state_degrades(
    writers: list[dict[str, Any]], warnings: list[str]
) -> None:
    """上一个事件的 cutoff 超出 state 消息数（越界）→ 告警降级，不切片。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage("a" * 200, id="m1"), HumanMessage("b" * 200, id="m2")]

    await tap.awrap_model_call(
        _request(
            [HumanMessage("new summary", id="summary-2")],
            state={"messages": raw, SUMMARIZATION_EVENT_KEY: _event(9)},
        ),
        _plain_handler,
    )

    assert writers == []
    assert any("超出 state 消息数" in message for message in warnings)


async def test_empty_request_window_degrades(writers: list[dict[str, Any]]) -> None:
    """空请求窗口（异常装配形态）→ 不做压缩判定。"""
    tap = LCodeEventTapMiddleware()

    await tap.awrap_model_call(
        _request([], state={"messages": [HumanMessage("a", id="m1")]}), _plain_handler
    )

    assert writers == []


async def test_window_longer_than_effective_before_degrades(
    writers: list[dict[str, Any]],
) -> None:
    """入口窗口比重建窗口更长（不是摘要改写）→ 不发事件。"""
    tap = LCodeEventTapMiddleware()
    raw = [HumanMessage(f"old-{index}", id=f"m{index}") for index in range(4)]
    prior_event = _event(3, summary=HumanMessage("prior", id="summary-0"))
    # 重建窗口 = [prior, raw[3:]]（2 条）；请求 4 条 → eff_cutoff <= 0
    request = _request(
        [HumanMessage("new", id="summary-9"), *raw[1:], HumanMessage("extra", id="m9")],
        state={"messages": raw, SUMMARIZATION_EVENT_KEY: prior_event},
    )

    await tap.awrap_model_call(request, _plain_handler)

    assert writers == []


async def test_result_without_command_or_event_degrades(
    writers: list[dict[str, Any]],
) -> None:
    """ExtendedModelResponse 无 command / update 无事件键 → 零事件（不是压缩）。"""
    tap = LCodeEventTapMiddleware()
    request = _request([HumanMessage("x", id="m1")])

    async def no_command(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return ExtendedModelResponse(model_response=ModelResponse(result=[]), command=None)

    async def empty_update(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return ExtendedModelResponse(
            model_response=ModelResponse(result=[]), command=Command(update={})
        )

    await tap.awrap_model_call(request, no_command)
    await tap.awrap_model_call(request, empty_update)

    assert writers == []


async def test_snapshot_failure_degrades(
    writers: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """入口快照异常 → 模型调用照常，零事件（检测面故障不拖垮 run）。"""
    tap = LCodeEventTapMiddleware()

    def _boom(_: ModelRequest[Any]) -> Any:
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(middleware_module, "_snapshot_model_call", _boom)

    result = await tap.awrap_model_call(_request([HumanMessage("x", id="m1")]), _plain_handler)

    assert isinstance(result, ModelResponse)
    assert writers == []


async def test_detection_failure_degrades(
    writers: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """检测阶段异常 → 模型调用照常，零事件。"""
    tap = LCodeEventTapMiddleware()

    def _boom(_: ModelRequest[Any], __: Any) -> Any:
        raise RuntimeError("detect boom")

    monkeypatch.setattr(middleware_module, "_detect_in_call", _boom)
    monkeypatch.setattr(middleware_module, "_detect_from_result", _boom)

    result = await tap.awrap_model_call(_request([HumanMessage("x", id="m1")]), _plain_handler)

    assert isinstance(result, ModelResponse)
    assert writers == []


async def test_compact_events_reach_custom_stream_through_real_graph() -> None:
    """真实 langgraph 图：外层摘要改写窗口 → tap 经 `get_stream_writer` 写进 custom 流。

    这是「模型节点内 writer 真实可用」的接线证据（其余用例猴补了 writer）；
    组装顺序与 deepagents 实际栈一致：摘要中间件在外、tap 在内。
    """
    from langchain.agents import create_agent

    class _FakeSummarizer(AgentMiddleware):
        """模拟摘要中间件：首次调用把窗口改写成 `[summary, last]`（外层）。"""

        name = "_FakeSummarizer"

        def __init__(self) -> None:
            self.done = False

        async def awrap_model_call(
            self,
            request: ModelRequest[Any],
            handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
        ) -> Any:
            if not self.done and len(request.messages) >= 2:
                self.done = True
                return await handler(request.override(messages=[_SUMMARY, request.messages[-1]]))
            return await handler(request)

    agent = create_agent(
        model=FakeListChatModel(responses=["ok"]),
        tools=[],
        middleware=[_FakeSummarizer(), LCodeEventTapMiddleware()],
    )
    raws = [
        raw
        async for raw in agent.astream(
            {"messages": [HumanMessage("one", id="m1"), HumanMessage("two", id="m2")]},
            stream_mode=["custom"],
        )
    ]

    payloads = [payload for _, payload in cast(Any, raws)]
    assert [payload["subject"] for payload in payloads] == ["compact", "compact"]
    assert [payload["phase"] for payload in payloads] == ["started", "finished"]
    assert payloads[1]["compact"]["after_tokens"] > 0


# ---------------------------------------------------------------- 等价真实触发（安装版摘要中间件）


class _StubBackend:
    """`_offload_to_backend` 用到的最小内存面（spike §3 同款；零网络）。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def download_files(self, paths: list[str]) -> list[Any]:
        return [
            SimpleNamespace(content=self.files[path].encode(), error=None)
            if path in self.files
            else SimpleNamespace(content=None, error="not_found")
            for path in paths
        ]

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        return [SimpleNamespace(path=path, error=None) for path, _ in files]

    def write(self, path: str, content: str) -> Any:
        self.files[path] = content
        return SimpleNamespace(error=None)

    def edit(self, path: str, old: str, new: str) -> Any:
        self.files[path] = self.files[path].replace(old, new)
        return SimpleNamespace(error=None)

    # async 摘要走 a* 面（`_aoffload_to_backend`，summarization.py:1310-1372）
    async def adownload_files(self, paths: list[str]) -> list[Any]:
        return self.download_files(paths)

    async def awrite(self, path: str, content: str) -> Any:
        return self.write(path, content)

    async def aedit(self, path: str, old: str, new: str) -> Any:
        return self.edit(path, old, new)


async def test_real_summarization_middleware_trigger_emits_compact(
    writers: list[dict[str, Any]],
) -> None:
    """安装版摘要中间件真实压缩（假摘要模型）→ tap 在内层检出并发出合法数值。

    组装顺序与 deepagents 实际栈一致：摘要中间件在外、tap 在内
    （`graph.py:776-779` 默认栈先于 `graph.py:811-812` 用户 middleware）。
    """
    summary_model = FakeListChatModel(responses=["real-ish summary text"])
    summarization = SummarizationMiddleware(
        model=summary_model,
        backend=cast(Any, _StubBackend()),  # 最小内存面；BackendProtocol 的静态形状从宽
        trigger=("messages", 2),
        keep=("messages", 1),
        truncate_args_settings=None,
    )
    tap = LCodeEventTapMiddleware()
    raw = [
        HumanMessage("filler " * 300, id="m1"),
        HumanMessage("hi", id="m2"),
        HumanMessage("more", id="m3"),
        HumanMessage("bye", id="m4"),
    ]
    seen: list[ModelRequest[Any]] = []

    async def inner(request: ModelRequest[Any]) -> ModelResponse[Any]:
        seen.append(request)
        return await tap.awrap_model_call(request, _plain_handler)

    outer = await summarization.awrap_model_call(_request(raw), inner)

    assert isinstance(outer, ExtendedModelResponse)  # 真实摘要确实产出了事件
    update = cast(dict[str, Any], cast(Any, outer).command.update)
    event = update[SUMMARIZATION_EVENT_KEY]
    assert event["cutoff_index"] > 0
    assert _phases(writers) == ["started", "finished"]
    info = _compact(writers)
    assert info["before_tokens"] == count_tokens_approximately(raw)
    assert info["after_tokens"] == count_tokens_approximately(seen[0].messages)
    assert info["before_tokens"] > info["after_tokens"] > 0

    # 下一次调用（state 已带该事件、窗口 = [summary] + state[cutoff:]）不重复发
    emitted = list(writers)
    effective = [event["summary_message"], *raw[event["cutoff_index"] :]]
    await tap.awrap_model_call(
        _request(effective, state={"messages": raw, SUMMARIZATION_EVENT_KEY: event}),
        _plain_handler,
    )
    assert writers == emitted
