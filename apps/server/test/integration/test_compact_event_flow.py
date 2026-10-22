"""integration：compact 事件链路（middleware → translator → EventPipeline → DB/回放）。

M3 Phase 5 / AC-010-1 的持久化半边：`run_status(subject=compact)` 必须与其它域事件
同源落库、按 seq 可回放，且 finished 的 `CompactInfo` 在 jsonb 往返后仍是契约形状。

等价触发说明（真实摘要触发成本高：需要真 LLM + 阈值逼近的上下文）：
- 用例 1 走 **结果事件路径**（m3-spikes §3 主路径）：直接构造带 `_summarization_event`
  的 `ExtendedModelResponse` 作为 middleware 的模型响应；
- 用例 2 走 **入口窗口路径**（安装版栈序下实际生效）：请求已是外层摘要改写后的
  effective 窗口（`[summary, *preserved]`），middleware 按 state 重建窗口判定。
两条路都真实经过 `EventTranslator` 的 custom 通道与 `EventPipeline` 的契约闸门、
seq 分配、落库与重放，**未覆盖**的部分是「真模型把上下文顶到阈值」这一步本身
（unit 层 `test_real_summarization_middleware_trigger_emits_compact` 用安装版摘要
中间件 + 假摘要模型补了中间件真实产出事件的等价面）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command
from pydantic import TypeAdapter
from sqlalchemy import select

import app.agent.middleware as middleware_module
from app.agent.middleware import SUMMARIZATION_EVENT_KEY, LCodeEventTapMiddleware
from app.events.bus import SessionEventBus
from app.events.emitter import EventPipeline
from app.events.replay import fetch_events_after
from app.events.translator import EventTranslator
from app.models import Event as EventRow
from app.models import Session as SessionRow
from lcode_contracts.events import Event

pytestmark = pytest.mark.requires_db

_EVENT_SOURCE: Any = Event
_ADAPTER: TypeAdapter[Any] = TypeAdapter(_EVENT_SOURCE)
_MODEL = FakeListChatModel(responses=["ok"])
_SUMMARY = HumanMessage("compacted summary of earlier turns", id="summary-1")


def _request(messages: Sequence[Any], *, state: Mapping[str, Any]) -> ModelRequest[Any]:
    return ModelRequest(
        model=_MODEL,
        messages=list(messages),
        system_message=SystemMessage("sys"),
        tools=[],
        state=cast(Any, state),  # AgentState TypedDict 不认私有键（运行期可见）
        runtime=None,
    )


async def _plain_handler(_: ModelRequest[Any]) -> ModelResponse[Any]:
    return ModelResponse(result=[])


async def _seed_session(db_session: Any) -> str:
    row = SessionRow(model="qwen3.7-plus", reasoning_effort="medium", sandbox_provider="bailian")
    db_session.add(row)
    await db_session.commit()
    return str(row.id)


async def _drain(
    payloads: list[dict[str, Any]], pipeline: EventPipeline, translator: EventTranslator
) -> list[Any]:
    """tap 载荷 → translator（custom 通道）→ EventPipeline（校验/seq/落库/广播）。"""
    stored: list[Any] = []
    for payload in payloads:
        for event in translator.translate(("custom", payload)):
            stored.append(await pipeline.emit(event))
    return stored


async def test_result_path_compact_events_persist_and_replay(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构造带 `_summarization_event` 的响应 → 两条 run_status 落库 + 可回放。"""
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    translator = EventTranslator(session_id=session_id)
    subscription = pipeline.bus.subscribe(session_id)
    payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(middleware_module, "get_stream_writer", lambda: payloads.append)

    messages = [HumanMessage("x" * 400, id="m1"), HumanMessage("y" * 400, id="m2")]
    event = {"cutoff_index": 1, "summary_message": _SUMMARY, "file_path": None}

    async def handler(_: ModelRequest[Any]) -> ExtendedModelResponse[Any]:
        return ExtendedModelResponse(
            model_response=ModelResponse(result=[]),
            command=Command(update={SUMMARIZATION_EVENT_KEY: event}),
        )

    await LCodeEventTapMiddleware().awrap_model_call(
        _request(messages, state={"messages": messages}), handler
    )
    stored = await _drain(payloads, pipeline, translator)

    assert [item.type for item in stored] == ["run_status", "run_status"]
    assert [item.phase for item in stored] == ["started", "finished"]
    assert stored[0].seq == 0 and stored[1].seq == 1  # 会话内单调递增

    # 总线（实时流）两条按序可见
    broadcast = [cast(Any, await subscription.get()), cast(Any, await subscription.get())]
    assert [item.type for item in broadcast] == ["run_status", "run_status"]
    await subscription.aclose()

    # 落库行：data jsonb 存非信封载荷（compact 前后窗口）
    rows = list(
        await db_session.scalars(
            select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.seq)
        )
    )
    assert [row.type for row in rows] == ["run_status", "run_status"]
    assert rows[0].data["subject"] == "compact" and rows[0].data["phase"] == "started"
    assert rows[1].data["phase"] == "finished"
    expected_info = {
        "before_tokens": count_tokens_approximately(messages),
        "after_tokens": count_tokens_approximately([_SUMMARY, *messages[1:]]),
    }
    assert rows[1].data["compact"] == expected_info

    # 回放（重放与实时同源）：seq 升序 + 契约联合校验 + 数值保真
    replayed = cast(Any, await fetch_events_after(session_id, after_seq=-1))
    assert [item.seq for item in replayed] == [0, 1]
    for item in replayed:
        _ADAPTER.validate_python(item.model_dump(mode="python"))
    assert [item.phase for item in replayed] == ["started", "finished"]
    assert replayed[1].compact.before_tokens == expected_info["before_tokens"]
    assert replayed[1].compact.after_tokens == expected_info["after_tokens"]


async def test_in_call_compact_events_persist_with_reconstructed_window(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安装版栈序等价触发：入口窗口 = `[summary, *preserved]` → 落库/回放同一契约。"""
    session_id = await _seed_session(db_session)
    pipeline = EventPipeline(bus=SessionEventBus(queue_size=8))
    translator = EventTranslator(session_id=session_id, run_id=None)
    payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(middleware_module, "get_stream_writer", lambda: payloads.append)

    raw = [HumanMessage(f"old-{index}" * 100, id=f"m{index}") for index in range(4)]
    compacted = [_SUMMARY, raw[-1]]
    tap = LCodeEventTapMiddleware()

    await tap.awrap_model_call(_request(compacted, state={"messages": raw}), _plain_handler)
    stored = await _drain(payloads, pipeline, translator)

    assert [item.phase for item in stored] == ["started", "finished"]
    rows = list(
        await db_session.scalars(
            select(EventRow).where(EventRow.session_id == session_id).order_by(EventRow.seq)
        )
    )
    assert rows[1].data["compact"] == {
        "before_tokens": count_tokens_approximately(raw),
        "after_tokens": count_tokens_approximately(compacted),
    }

    # 同一压缩不重复发：下一次调用（state 已带事件、窗口首条即该 summary）落 0 条
    new_event = {"cutoff_index": 3, "summary_message": _SUMMARY, "file_path": None}
    payloads.clear()
    await tap.awrap_model_call(
        _request([_SUMMARY, *raw[3:]], state={"messages": raw, SUMMARIZATION_EVENT_KEY: new_event}),
        _plain_handler,
    )
    assert payloads == []
    assert await _drain(payloads, pipeline, translator) == []
