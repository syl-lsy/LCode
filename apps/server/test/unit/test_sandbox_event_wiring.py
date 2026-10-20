"""unit 层：沙箱事件桥接与 exec_id 注解链（M2 集成装配的跨车道接缝）。

覆盖三段链路的最小单元面（真库/真总线走 integration）：

1. dependencies 的两个载荷构造器：相位迁移/输出块 → 契约 `SandboxOutputEvent`
   （过 `validate_event_payload` 闸门；无 sandbox_id / 无 exec_id 的诚实跳过）；
2. tap 中间件 → `EXEC_ID_CONTEXT` 的注解与复位（deepagents 0.6.12 的 execute 工具
   不传 exec_id，注解靠本上下文接力——见 `app/utils/exec_context.py` docstring）；
3. `LcodeSandbox.run_command` 的 exec_id 回退（显式参数优先，上下文兜底）；
4. `build_sandbox_usage` 纯构造（ORM 行 + 费率口径的估算金额）；
5. main.py 的空闲清扫循环与 lifespan 启停（stub manager，零 DB）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from app.agent.middleware import LCodeEventTapMiddleware
from app.dependencies import (
    build_sandbox_event_bridges,
    sandbox_lifecycle_payload,
    sandbox_output_payload,
)
from app.events import validate_event_payload
from app.main import IDLE_SWEEP_INTERVAL_S, app, idle_sweeper, lifespan
from app.sandbox.base import SandboxUsageSegment
from app.sandbox.manager import SandboxLifecycleEvent, SandboxUsageRecord
from app.services.cost_service import build_sandbox_usage
from app.utils.exec_context import EXEC_ID_CONTEXT
from test.support.fake_sandbox import FakeSandbox

# --------------------------------------------------------------------------- 载荷构造器

SESSION = str(uuid4())


def _lifecycle(phase: str = "running", sandbox_id: str | None = "sbx-1") -> SandboxLifecycleEvent:
    return SandboxLifecycleEvent(
        session_id=SESSION,
        phase=phase,  # type: ignore[arg-type]
        provider="bailian",
        sandbox_id=sandbox_id,
        reason="manual",
        at=datetime.now(UTC),
    )


async def test_lifecycle_payload_passes_contract_gate() -> None:
    """有 sandbox_id 的迁移 → 合法 `sandbox_output(lifecycle)` 事件（过唯一闸门）。"""
    payload = sandbox_lifecycle_payload(_lifecycle("paused"))
    assert payload is not None
    event = cast(Any, validate_event_payload(payload))  # 不过契约校验即失败
    assert event.type == "sandbox_output"
    assert event.channel == "lifecycle"
    assert event.phase == "paused"
    assert event.provider == "bailian"
    assert event.sandbox_id == "sbx-1"


async def test_lifecycle_payload_skips_without_sandbox_id() -> None:
    """creating 相位（实例未 provision）/none 相位无 id 可引用 → None（不造占位值）。"""
    assert sandbox_lifecycle_payload(_lifecycle("creating", sandbox_id=None)) is None
    assert sandbox_lifecycle_payload(_lifecycle("none", sandbox_id=None)) is None


async def test_output_payload_passes_contract_gate_and_skips_without_exec_id() -> None:
    """带 exec_id 的输出块 → 合法 stdout 事件；无 exec_id（无工具上下文）→ None。"""
    from app.sandbox.manager import SandboxOutputChunk

    payload = sandbox_output_payload(SESSION, "sbx-1", SandboxOutputChunk("stdout", "hi", "t-1"))
    assert payload is not None
    event = cast(Any, validate_event_payload(payload))
    assert event.channel == "stdout"
    assert event.data == "hi"
    assert event.exec_id == "t-1"
    assert event.sandbox_id == "sbx-1"

    assert (
        sandbox_output_payload(SESSION, "sbx-1", SandboxOutputChunk("stdout", "hi", None)) is None
    )


# --------------------------------------------------------------------------- 桥接闭包


async def test_bridges_launch_emit_tasks_on_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """桥接闭包在有回路时把 emit 挂到后台任务（记录型 pipeline 断言载荷到达）。"""

    class RecordingPipeline:
        def __init__(self) -> None:
            self.emitted: list[Any] = []

        async def emit(self, payload: Any) -> Any:
            self.emitted.append(payload)
            return payload

    pipeline = RecordingPipeline()
    on_lifecycle, _on_output, _on_usage = build_sandbox_event_bridges(pipeline)  # type: ignore[arg-type]
    on_lifecycle(_lifecycle("killed"))
    await asyncio.sleep(0)  # 排队任务跑完
    assert [e.phase for e in pipeline.emitted] == ["killed"]


# --------------------------------------------------------------------------- exec_id 注解链


def _tap_request(tool_call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "execute", "args": {"command": "echo hi"}, "id": tool_call_id},
        tool=None,
        state=None,
        runtime=cast(Any, None),  # 图外直构造：runtime 无消费面，Any 压制 ToolRuntime 泛型
    )


async def test_tap_sets_and_resets_exec_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """tap 在 handler 执行期间把 tool_call_id 写入 EXEC_ID_CONTEXT，结束后复位。"""
    seen: list[str | None] = []
    writers: list[dict[str, Any]] = []

    async def handler(request: ToolCallRequest) -> Any:
        seen.append(EXEC_ID_CONTEXT.get())
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    monkeypatch.setattr("app.agent.middleware.get_stream_writer", lambda: writers.append)

    token = EXEC_ID_CONTEXT.set(None)
    try:
        result = await LCodeEventTapMiddleware().awrap_tool_call(_tap_request("call-9"), handler)
    finally:
        EXEC_ID_CONTEXT.reset(token)

    assert seen == ["call-9"]  # handler 期间可见
    assert EXEC_ID_CONTEXT.get() is None  # 结束后复位（不泄漏到后续工具调用）
    assert result.content == "ok"
    assert [w["type"] for w in writers] == ["tool_call_started", "tool_call_finished"]


async def test_run_command_falls_back_to_context_exec_id(tmp_path: Any) -> None:
    """exec_id 显式参数优先；缺省时回退 EXEC_ID_CONTEXT（deepagents 工具路径）；无上下文 = None。"""
    client = FakeSandbox(root=tmp_path / "sbx")
    seen: list[Any] = []
    client.add_output_handler(seen.append)

    token = EXEC_ID_CONTEXT.set("call-ctx")
    try:
        client.run_command("echo explicit", exec_id="call-explicit")
        client.run_command("echo fallback")
    finally:
        EXEC_ID_CONTEXT.reset(token)
    client.run_command("echo none")

    assert [chunk.exec_id for chunk in seen] == ["call-explicit", "call-ctx", None]


# --------------------------------------------------------------------------- usage 落库构造


async def test_build_sandbox_usage_maps_record_to_row() -> None:
    """SandboxUsageRecord → sandbox_usage ORM 行：字段映射 + 费率口径估算金额。"""
    started = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
    record = SandboxUsageRecord(
        session_id=SESSION,
        provider="bailian",
        sandbox_id="sbx-1",
        segment=SandboxUsageSegment(
            phase="running",
            started_at=started,
            ended_at=datetime(2026, 10, 18, 12, 10, 0, tzinfo=UTC),
            duration_seconds=600,
        ),
    )
    row = build_sandbox_usage(record)
    assert str(row.session_id) == SESSION
    assert row.provider == "bailian"
    assert row.sandbox_id == "sbx-1"
    assert row.phase == "running"
    assert row.duration_seconds == 600
    # 600s × 0.156 元/h = 0.026（cost_service 单一费率源）
    assert row.estimated_cost == pytest.approx(0.026, abs=1e-6)
    assert row.currency == "CNY"


# --------------------------------------------------------------------------- lifespan 装配


class _StubManager:
    """lifespan 装配测试的桩：记录 idle/shutdown 调用序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate_idle(self) -> tuple[str, ...]:
        self.calls.append("idle")
        return ()

    def shutdown(self) -> None:
        self.calls.append("shutdown")


async def test_idle_sweeper_loops_until_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """清扫循环按间隔反复 evaluate_idle，取消即退出（不吞 CancelledError）。"""
    monkeypatch.setattr("app.main.IDLE_SWEEP_INTERVAL_S", 0.01)
    stub = _StubManager()
    task = asyncio.create_task(idle_sweeper(stub))  # type: ignore[arg-type]
    await asyncio.sleep(0.06)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert stub.calls.count("idle") >= 2
    assert task.cancelled()


async def test_lifespan_starts_sweeper_and_pauses_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lifespan：启动期起清扫循环，退出期先停循环再 shutdown pause（顺序不可调换）。"""
    stub = _StubManager()
    monkeypatch.setattr("app.main.get_sandbox_manager", lambda: stub)
    monkeypatch.setattr("app.main.IDLE_SWEEP_INTERVAL_S", 0.01)

    async def _noop_dispose() -> None:
        return None

    monkeypatch.setattr("app.main.dispose_engine", _noop_dispose)

    async with lifespan(app):
        await asyncio.sleep(0.06)  # 留时间给清扫循环至少跑一轮

    assert stub.calls.count("idle") >= 1
    assert stub.calls[-1] == "shutdown"


def test_sweep_interval_constant_is_sane() -> None:
    """清扫间隔远小于空闲阈值（保证 evaluate_idle 的判定粒度不被清扫周期放大）。"""
    from app.core.config import sandbox_settings

    assert 0 < IDLE_SWEEP_INTERVAL_S <= sandbox_settings.idle_pause_seconds / 2
