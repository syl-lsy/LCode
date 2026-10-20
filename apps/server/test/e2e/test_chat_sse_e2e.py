"""M2 演示链 e2e（T7；test-plan-m2 §1.2/§2/§5）：发消息 → SSE 流式 → 沙箱输出实时 → 刷新重放。

链路面（假模型 + FakeSandbox + 真 AsyncPostgresSaver + 真库，见 backend-design §9.2）：
子进程真服务（conftest 起停，经 server_entry 按 `LCODE_E2E_FAKE_LLM` /
`LCODE_E2E_FAKE_SANDBOX` / `LCODE_E2E_CHECKPOINTER` 装配替身与真库 checkpointer），
本文件只经 HTTP/SSE 与它对话——**不 import app**（进程边界是 e2e 的立意）。

每条 SSE `data` 都过 `TypeAdapter(Event)` 判别联合校验 + P0 事件集合守卫
（无原始 LangGraph 事件泄漏，AC-008-2 的链路级断言）；SSE 帧 `id` 与载荷 `seq` 一致。

场景路由（假模型按用户消息内容选剧本，见 e2e/fake_runtime.py）：
`长文`→LONG_500 / `执行命令`→TOOL_EXECUTE / `失败的命令`→TOOL_FAIL /
`长输出`→STOP_STREAM / `第一轮`·`刚才`→MULTI_TURN / 其余→SIMPLE_REPLY。
"""

import asyncio
import json
from collections.abc import Callable
from time import monotonic
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from lcode_contracts.events import P0_EVENT_TYPES, Event
from lcode_contracts.rest import (
    MessagePage,
    MessageSendAck,
    SessionCost,
    SessionDetail,
    StopRunAck,
)
from test.support import scripts

pytestmark = pytest.mark.asyncio

_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Event)  # type: ignore[arg-type]
"""SSE 出口载荷的唯一闸门（ADR-003；与 emitter/replay 同一判别联合）。"""

_RUN_TERMINAL: tuple[str, ...] = ("finished", "stopped", "error")
"""run_status(subject=run) 的终态相位（read_until 的收口判据）。"""


# ------------------------------------------------------------------ REST 小工具


def _unwrap(response: httpx.Response) -> dict[str, Any]:
    """`/api/` 成功信封解包：`{"code":"0","data":...}` → data（非 0/非 2xx 直接红）。"""
    assert response.status_code in (200, 201), (
        f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["code"] == "0", f"业务码非 0：{body}"
    return body["data"]


async def create_session(client: httpx.AsyncClient) -> SessionDetail:
    """创建会话（契约 SessionDetail 校验；默认模型/档位由服务端注册表落）。"""
    response = await client.post("/api/v1/sessions", json={})
    return SessionDetail.model_validate(_unwrap(response))


async def send_message(client: httpx.AsyncClient, session_id: str, content: str) -> MessageSendAck:
    """发送消息（契约 MessageSendAck 校验；event_seq 是订阅水位）。"""
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages", json={"content": content}
    )
    return MessageSendAck.model_validate(_unwrap(response))


async def stop_run(client: httpx.AsyncClient, run_id: str) -> StopRunAck:
    """停止运行（契约 StopRunAck 校验：status 恒 stopped）。"""
    response = await client.post(f"/api/v1/runs/{run_id}/stop")
    return StopRunAck.model_validate(_unwrap(response))


async def list_messages(client: httpx.AsyncClient, session_id: str) -> MessagePage:
    """消息历史（契约 MessagePage 校验）。"""
    response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    return MessagePage.model_validate(_unwrap(response))


async def session_cost(client: httpx.AsyncClient, session_id: str) -> SessionCost:
    """会话成本（契约 SessionCost 校验：model 段 + sandbox 段）。"""
    response = await client.get(f"/api/v1/sessions/{session_id}/costs")
    return SessionCost.model_validate(_unwrap(response))


# ------------------------------------------------------------------ SSE 客户端


def _validate_wire(payload: dict[str, Any]) -> dict[str, Any]:
    """单条 SSE data 的契约校验：判别联合 + P0 集合 + seq 下界（AC-008-2）。"""
    event = _EVENT_ADAPTER.validate_python(payload)
    assert payload["type"] in P0_EVENT_TYPES, f"非 P0 事件类型泄漏：{payload['type']!r}"
    assert payload["seq"] >= 0
    return event.model_dump(mode="json")


class SSEStream:
    """阻塞式 SSE 读取器：逐帧解析 `id/event/data`，每条 data 过契约校验。

    帧格式见契约 rest.py 模块 docstring（retry 首帧 / `: keep-alive` 心跳注释行 /
    `id: <seq>` / `event: <type>` / `data: <单行 JSON>`）。断线续传水位：
    `Last-Event-ID` 头优先（不传即全量重放，服务端水位 -1）。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        last_event_id: int | None = None,
    ) -> None:
        self._client = client
        self._path = f"/api/v1/sessions/{session_id}/events"
        self._headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
        self._cm: Any = None
        self._lines: Any = iter(())  # httpx aiter_lines()（__aenter__ 后就位）
        self.retry_ms: int | None = None
        self.first_frame_seen = False

    async def __aenter__(self) -> SSEStream:
        self._cm = self._client.stream("GET", self._path, headers=self._headers)
        response = await self._cm.__aenter__()
        assert response.status_code == 200, f"SSE 建连失败：{response.status_code}"
        assert response.headers["content-type"].startswith("text/event-stream")
        self._lines = response.aiter_lines()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._cm.__aexit__(*exc_info)

    async def _next_frame(self, timeout: float) -> tuple[str | None, str | None, str | None]:
        """读一帧（空行分帧）；注释/纯 retry 帧返回 (None, None, None)。"""
        frame_id: str | None = None
        frame_type: str | None = None
        data: str | None = None
        while True:
            line = await asyncio.wait_for(self._lines.__anext__(), timeout=timeout)
            if line.startswith(":"):
                continue  # 心跳注释行
            if line.startswith("retry:"):
                self.retry_ms = int(line.removeprefix("retry:").strip())
                continue
            if line.startswith("id:"):
                frame_id = line.removeprefix("id:").strip()
                self.first_frame_seen = True
            elif line.startswith("event:"):
                frame_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = line.removeprefix("data:").strip()
            elif line == "":
                if data is not None:
                    return frame_id, frame_type, data
                continue  # 纯 retry 帧 / 空行，不是事件
            if line and not line.startswith(("id:", "event:", "data:")):
                raise AssertionError(f"SSE 帧出现未知行：{line!r}")

    async def next_event(self, timeout: float = 30.0) -> dict[str, Any]:
        """读下一条契约事件（心跳/retry 帧跳过；帧 id 与载荷 seq 一致性在此断言）。"""
        frame_id, frame_type, data = await self._next_frame(timeout)
        assert data is not None and frame_type is not None, "事件帧必须携带 event/data 行"
        payload = json.loads(data)
        event = _validate_wire(payload)
        assert frame_id is not None and int(frame_id) == payload["seq"], (
            f"SSE id 与载荷 seq 不一致：id={frame_id} seq={payload['seq']}"
        )
        assert payload["type"] == frame_type, f"event 行与载荷 type 不一致：{frame_type}"
        return event

    async def read_until(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 90.0,
    ) -> list[dict[str, Any]]:
        """顺序消费事件直到谓词命中（含命中事件）；超时 AssertionError。"""
        deadline = monotonic() + timeout
        collected: list[dict[str, Any]] = []
        while True:
            remaining = deadline - monotonic()
            assert remaining > 0, f"read_until 超时（已收 {len(collected)} 条事件）"
            event = await self.next_event(timeout=min(remaining, 30.0))
            collected.append(event)
            if predicate(event):
                return collected

    async def drain(self, seconds: float = 1.5) -> list[dict[str, Any]]:
        """终态后短窗补收（静默即停）。

        工具线程经主循环桥投递的尾部事件（如最后一块 stdout）可能晚于 run 终态广播
        才完成落库——连接保持打开时它们仍在订阅队列里，真实前端就是继续读。
        """
        collected: list[dict[str, Any]] = []
        deadline = monotonic() + seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return collected
            try:
                collected.append(await self.next_event(timeout=remaining))
            except TimeoutError, StopAsyncIteration:
                return collected


def _is_run_terminal(event: dict[str, Any]) -> bool:
    """run_status(subject=run) 终态判定。"""
    return (
        event["type"] == "run_status"
        and event["subject"] == "run"
        and event["phase"] in _RUN_TERMINAL
    )


def _seqs(events: list[dict[str, Any]]) -> list[int]:
    return [event["seq"] for event in events]


def _assert_strictly_increasing(events: list[dict[str, Any]]) -> None:
    seqs = _seqs(events)
    assert seqs == sorted(seqs), "事件 seq 非单调递增"
    assert len(set(seqs)) == len(seqs), "事件 seq 出现重复（重放/实时去重失效）"


async def _completed_turn(
    client: httpx.AsyncClient, content: str, *, timeout: float = 90.0
) -> tuple[SessionDetail, MessageSendAck, list[dict[str, Any]]]:
    """全链一次：建会话 → 开 SSE → 发消息 → 收到 run 终态（返回全部事件）。"""
    session = await create_session(client)
    async with SSEStream(client, session.id) as stream:
        ack = await send_message(client, session.id, content)
        events = await stream.read_until(_is_run_terminal, timeout=timeout)
    return session, ack, events


# ------------------------------------------------------------------ 用例（test-plan §2 映射）


async def test_send_message_streams_reply(api_client: httpx.AsyncClient) -> None:
    """AC-001-1 [e2e]：POST 消息 → message_created → delta → finished 的完整流。"""
    session, ack, events = await _completed_turn(api_client, scripts.SIMPLE_USER_TEXT)

    assert ack.event_seq >= 0  # 契约 MessageSendAck：水位下界（M1 fixture 锚）
    types = [event["type"] for event in events]
    assert types[0] == "message_created", "全量重放（无水位）首条即用户消息回声"
    assert "message_delta" in types and "message_finished" in types

    created = events[0]
    assert created["role"] == "user"
    assert created["content"] == scripts.SIMPLE_USER_TEXT
    assert created["message_id"] == ack.message_id
    assert types.index("message_created") < types.index("message_delta"), (
        "用户回声必须先于任何 assistant 增量"
    )

    text = "".join(
        event["delta"]
        for event in events
        if event["type"] == "message_delta"
        if event["kind"] == "text"
    )
    assert scripts.SIMPLE_REPLY_TEXT in text

    finished = [event for event in events if event["type"] == "message_finished"]
    assert finished, "必须有 assistant 收口事件"
    last_finished = finished[-1]
    assert last_finished["finish_reason"] == "stop"
    assert last_finished["usage"] == {"input_tokens": 12, "output_tokens": 7}, (
        "剧本 usage 必须随 finished 透出（AC-015-1 口径）"
    )

    statuses = [
        event for event in events if event["type"] == "run_status" and event["subject"] == "run"
    ]
    assert statuses, "必须发 run_status(started)（entry 节点 custom 流）"
    assert statuses[0]["phase"] == "started" and statuses[-1]["phase"] == "finished"
    _assert_strictly_increasing(events)


async def test_token_chunk_order(api_client: httpx.AsyncClient) -> None:
    """AC-003-1 [e2e]：≥500 token 剧本按序流出，拼接还原原文、seq 严格递增。"""
    session, _ack, events = await _completed_turn(
        api_client, "请输出一篇很长的长文（至少 500 token）。"
    )

    deltas = [
        event for event in events if event["type"] == "message_delta" and event["kind"] == "text"
    ]
    assert len(deltas) >= 500, f"长文剧本应产出 ≥500 条增量，实际 {len(deltas)}"
    assert all(event["delta"] for event in deltas), "增量不允许为空块"

    # 翻译层现状（run_service docstring 如实注明）：主图回声会再发一轮收口——按
    # message_id 分组断言「每条消息的增量顺序完整还原原文」，不锁全局拼接。
    by_message: dict[str, list[str]] = {}
    for event in deltas:
        by_message.setdefault(event["message_id"], []).append(event["delta"])
    per_message_texts = ["".join(pieces) for pieces in by_message.values()]
    assert any(text == scripts.LONG_500_TEXT for text in per_message_texts), (
        "按 message_id 分组后必须有一条消息的增量按序还原长文全文（顺序不乱）"
    )
    _assert_strictly_increasing(events)


async def test_sandbox_output_streams_to_terminal(api_client: httpx.AsyncClient) -> None:
    """AC-002-1 [e2e]：execute 工具 → sandbox_output(stdout, exec_id) 实时 → finished.exit_code。"""
    session = await create_session(api_client)
    async with SSEStream(api_client, session.id) as stream:
        await send_message(api_client, session.id, "帮我执行命令看看沙箱环境（执行命令）。")
        events = await stream.read_until(_is_run_terminal, timeout=60)
        events += await stream.drain(1.5)  # 工具线程桥的尾部 stdout 块可能晚于终态广播落库

    types = [event["type"] for event in events]
    assert "tool_call_started" in types and "tool_call_finished" in types

    # 回声去重已落地（translator 按上游 id 去重）——e2e 断言语义（同 id 的
    # started/finished/exit_code 配对），不锁发射条数。
    started = next(
        event
        for event in events
        if event["type"] == "tool_call_started" and event["tool"] == "execute"
    )
    assert started["args"]["command"] == scripts.EXEC_STREAM_COMMAND
    tool_call_id = started["tool_call_id"]

    outputs = [event for event in events if event["type"] == "sandbox_output"]
    stdout_chunks = [
        event for event in outputs if event["channel"] == "stdout" and event["exec_id"] is not None
    ]
    assert stdout_chunks, "stdout 输出必须带 exec_id（契约跨字段校验 + AC-002-1）"
    assert all(event["exec_id"] == tool_call_id for event in stdout_chunks), (
        "输出块必须标注发起命令的工具调用 id"
    )
    # 内容完整性（不锁块序）：并发发射下 seq 只保序不保密度，块间到达次序不构成契约
    streamed = "".join(event["data"] for event in stdout_chunks)
    for line in scripts.EXEC_STREAM_LINES:
        assert line in streamed, f"stdout 必须包含 {line!r}"

    exit_finished = next(
        event
        for event in events
        if event["type"] == "tool_call_finished"
        and event["tool_call_id"] == tool_call_id
        and event.get("exit_code") is not None
    )
    assert exit_finished["tool_call_id"] == tool_call_id
    assert exit_finished["exit_code"] == 0, "命令成功 → tool_call_finished.exit_code=0"

    lifecycle = [event for event in outputs if event["channel"] == "lifecycle"]
    assert any(event["phase"] == "running" for event in lifecycle), "沙箱 running 迁移对终端可见"


async def test_stop_keeps_partial_and_emits_run_stopped(api_client: httpx.AsyncClient) -> None:
    """AC-001-2 [e2e]：停止保留已生成内容 + message_finished(cancelled) + run_status(stopped)。"""
    session = await create_session(api_client)
    async with SSEStream(api_client, session.id) as stream:
        ack = await send_message(api_client, session.id, "开始一段很长的输出（长输出）。")

        # 流已真正开始（首个文本增量到达）才请求停止
        head = await stream.read_until(lambda event: event["type"] == "message_delta", timeout=30)
        partial_before_stop = "".join(
            event["delta"]
            for event in head
            if event["type"] == "message_delta" and event["kind"] == "text"
        )
        assert partial_before_stop, "停止前必须已有增量产出"

        stopped = await stop_run(api_client, ack.run_id)
        assert stopped.status == "stopped"

        tail = await stream.read_until(_is_run_terminal, timeout=30)
    events = head + tail

    cancelled = [
        event
        for event in events
        if event["type"] == "message_finished" and event["finish_reason"] == "cancelled"
    ]
    assert cancelled, "在飞消息必须补收口 message_finished(cancelled)"
    stopped_status = [
        event
        for event in events
        if event["type"] == "run_status"
        and event["subject"] == "run"
        and event["phase"] == "stopped"
    ]
    assert stopped_status, "run_status(subject=run, phase=stopped) 必须发出"

    # 已生成内容保留：停止后的消息历史含停止前的部分文本（AC-001-2 落库侧）
    history = await list_messages(api_client, session.id)
    assistant_texts = [item.content for item in history.items if item.role == "assistant"]
    assert any(partial_before_stop in text for text in assistant_texts if text), (
        "停止保留的已生成内容必须进入消息历史"
    )


async def test_reconnect_replays_without_gap(api_client: httpx.AsyncClient) -> None:
    """AC-003-3 [e2e]：断线 → Last-Event-ID 重连，重放无缺口无重复，与全量重放一致。"""
    session, _ack, _events = await _completed_turn(
        api_client, "请输出一篇很长的长文（至少 500 token）。"
    )

    async with SSEStream(api_client, session.id) as first:
        head = []
        for _ in range(5):
            head.append(await first.next_event())
    watermark = head[-1]["seq"]

    async with SSEStream(api_client, session.id, last_event_id=watermark) as resumed:
        rest = await resumed.read_until(_is_run_terminal, timeout=60)

    assert all(event["seq"] > watermark for event in rest), "重连段只发水位之后的事件"
    assert rest[0]["seq"] == watermark + 1, "重连首条紧接水位（无缺口）"
    _assert_strictly_increasing(head + rest)

    # 全量重放 = 两段拼接（刷新恢复与断线续传同源，AC-003-4/AC-008-3）
    async with SSEStream(api_client, session.id) as full:
        replayed = await full.read_until(_is_run_terminal, timeout=60)
        assert full.retry_ms is not None, "连接建立必须先发 retry 首帧"
    assert _seqs(replayed) == _seqs(head + rest), "全量重放与断线两段完全一致（不丢不重）"


async def test_refresh_replays_full_ui_state(api_client: httpx.AsyncClient) -> None:
    """AC-004-1 [e2e]：刷新（无水位全量重放）可重建完整 UI 状态，与实时流逐字节一致。"""
    session, _ack, live = await _completed_turn(
        api_client, "帮我执行命令看看沙箱环境（执行命令）。"
    )
    live_types = {event["type"] for event in live}
    assert {
        "message_created",
        "message_delta",
        "message_finished",
        "tool_call_started",
        "tool_call_finished",
        "sandbox_output",
        "run_status",
    } <= live_types, f"实时流缺少演示链关键事件族：{live_types}"

    async with SSEStream(api_client, session.id) as replay:
        replayed = await replay.read_until(_is_run_terminal, timeout=60)

    assert len(replayed) == len(live), "重放条数与实时流一致"
    assert replayed == live, "重放事件与实时流逐字段一致（created_at/seq 均为持久化事实）"
    # 重放段同样全量过契约校验（SSEStream 构造时逐条 validate）——无原始事件泄漏


async def test_refresh_then_continue_uses_history(api_client: httpx.AsyncClient) -> None:
    """AC-001-3/AC-004-2 [e2e]：刷新重放后续聊，历史（含第一轮）完整在案、第二轮正常。"""
    session = await create_session(api_client)
    async with SSEStream(api_client, session.id) as stream:
        await send_message(api_client, session.id, "这是第一轮对话（第一轮），记住话题。")
        turn1 = await stream.read_until(_is_run_terminal, timeout=30)
    turn1_watermark = turn1[-1]["seq"]

    # 「刷新」：无水位重连全量重放，确认第一轮历史完整
    async with SSEStream(api_client, session.id) as replay:
        replayed = await replay.read_until(_is_run_terminal, timeout=30)
    replayed_created = [event for event in replayed if event["type"] == "message_created"]
    assert any(
        event["content"] == "这是第一轮对话（第一轮），记住话题。" for event in replayed_created
    )

    # 续聊：刷新后的客户端带 Last-Event-ID（第一轮水位）重连，只收第二轮实时事件
    # ——不带水位会先重放第一轮历史，read_until 会在历史终态处提前返回。
    async with SSEStream(api_client, session.id, last_event_id=turn1_watermark) as stream:
        await send_message(api_client, session.id, "我们刚才聊了什么？")
        second_turn = await stream.read_until(_is_run_terminal, timeout=30)
    assert all(event["seq"] > turn1_watermark for event in second_turn)
    second_text = "".join(
        event["delta"]
        for event in second_turn
        if event["type"] == "message_delta" and event["kind"] == "text"
    )
    assert scripts.MULTI_TURN_SECOND_REPLY in second_text

    history = await list_messages(api_client, session.id)
    contents = [item.content for item in history.items]
    assert "这是第一轮对话（第一轮），记住话题。" in contents, "第一轮用户消息在历史"
    assert scripts.MULTI_TURN_FIRST_REPLY in contents, "第一轮 assistant 回复在历史"
    assert "我们刚才聊了什么？" in contents, "第二轮用户消息在历史"
    assert any(scripts.MULTI_TURN_SECOND_REPLY in text for text in contents), "第二轮回复在历史"


async def test_failed_tool_call_rendered_faithfully(api_client: httpx.AsyncClient) -> None:
    """AC-004-4 [e2e]：失败工具调用如实呈现（status=error + error 对象），重放不美化。"""
    session, _ack, live = await _completed_turn(
        api_client, "帮我执行一个会失败的命令（失败的命令）。"
    )

    failures = [
        event
        for event in live
        if event["type"] == "tool_call_finished" and event["status"] == "error"
    ]
    assert failures, "失败工具调用必须以 status=error 收口"
    failure = failures[0]
    # 契约 tool_call_finished 无 tool 字段——工具名经 tool_call_id 关联 started 事件解析
    started_by_id = {
        event["tool_call_id"]: event for event in live if event["type"] == "tool_call_started"
    }
    assert started_by_id.get(failure["tool_call_id"], {}).get("tool") == "execute"
    assert failure["error"] is not None, "status=error 必须携带 error 对象（契约耦合校验）"
    assert failure["error"]["code"], "error.code 必须非空"

    # 沙箱不可用是 run 级失败（AC-002-4 口径）：tap 发出失败卡片后异常继续上抛，run
    # 如实收 error 终态（不美化成 finished）——失败卡片与错误终态都必须可见。
    terminal = [event for event in live if _is_run_terminal(event)]
    assert terminal, "run 必须收敛到终态"
    assert terminal[-1]["phase"] == "error", "沙箱不可用 → run_status(subject=run, phase=error)"

    # 刷新重放：失败卡片如实重现（不消失、不美化）
    async with SSEStream(api_client, session.id) as replay:
        replayed = await replay.read_until(_is_run_terminal, timeout=60)
    replay_failures = [
        event
        for event in replayed
        if event["type"] == "tool_call_finished" and event["status"] == "error"
    ]
    assert replay_failures == failures, "重放的失败工具卡片与实时流逐字段一致"


async def test_cost_written_after_turn(api_client: httpx.AsyncClient) -> None:
    """AC-015-1 [e2e]：一轮结束后 model_calls 落库，会话成本端点读出模型段费用。"""
    session, _ack, events = await _completed_turn(api_client, "报一下这轮的成本（成本）。")

    finished = [event for event in events if event["type"] == "message_finished"]
    assert finished and finished[-1]["usage"] is not None, "收口事件带 usage"

    cost = await session_cost(api_client, session.id)
    assert cost.model.input_tokens > 0, "模型段 input_tokens 必须落库（剧本 12）"
    assert cost.model.output_tokens > 0, "模型段 output_tokens 必须落库（剧本 7）"
    assert cost.model_cost > 0, "按注册表单价估算的费用必须 > 0"
    assert cost.sandbox.rate_per_hour > 0, "沙箱段估算随会话成本返回（AC-015-3 两段结构）"
