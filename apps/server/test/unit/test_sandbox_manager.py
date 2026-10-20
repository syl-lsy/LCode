"""unit 层：SandboxManager 相位机 / 空闲 pause / 重建 / 计费分段 + 适配器薄翻译。

对应 test-plan-m2 §2.2 的 unit 列（T3）与 §2.6 的 AC-015-4：

| 用例 | AC |
|---|---|
| `test_create_load_execute_phase_machine` | AC-002-1（create→running、输出块回调、registry） |
| `test_run_command_streams_chunks_with_exec_id` | AC-002-1（stdout 增量 + exec_id 标注） |
| `test_idle_pause_fires_at_threshold` | AC-002-2（假时钟推进到阈值才 pause） |
| `test_idle_pause_skipped_while_run_active` | AC-002-2（Q3：run 活跃期间永不 pause） |
| `test_resume_preserves_registry` | AC-002-2（恢复沿用同一实例） |
| `test_usage_segments_running_and_paused` | AC-015-4（running/paused 分段 + 时长） |
| `test_session_delete_kills_sandbox` | AC-002-3（DELETE → kill） |
| `test_unavailable_marks_crashed_and_rebuild_restores` | AC-002-4（不可用→crashed→recreated） |
| `test_probe_marks_crashed_when_provider_reports_unavailable` | AC-002-4（被动探测路径） |
| `test_factory_provider_switch_only_new_sessions` | AC-002-6（provider 按会话固化） |
| `test_shutdown_pauses_running_sandboxes` | U9（关停 pause 而非 kill） |
| `test_illegal_transition_raises` | 相位机约束（设计 §4.3） |
| 适配器段（`test_bailian_*` / `test_opensandbox_*`） | 双适配器薄翻译的注入式单测（真 SDK 冒烟单列 requires_sandbox） |

时钟固定（test-plan §3.6）：本模块用 `FakeClock` patch `app.utils.clock.utc_now`，
空闲 pause 靠 `advance()` 推进；分段时长来自假时钟而非真墙钟。

命名说明：manager 的命令入口叫 `run_command`（`execute` 留给 deepagents 协议成员——
`LcodeSandbox.execute` 只是它的薄委托）；下方 `CDM_*` 常量是**喂给沙箱替身执行的
shell 命令原文**（替身在工作根目录真跑子进程），不是任何数据库语句。
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from e2b import AuthenticationException
from e2b.sandbox.commands.command_handle import CommandExitException

from app.core.config import SandboxSettings
from app.sandbox import factory as sandbox_factory
from app.sandbox.bailian import BailianSandbox
from app.sandbox.base import (
    LcodeSandbox,
    SandboxConfigurationError,
    SandboxLifecycleError,
    SandboxOutputChunk,
    SandboxProvider,
    SandboxUnavailable,
    SandboxUsageSegment,
)
from app.sandbox.manager import SandboxLifecycleEvent, SandboxManager, SandboxUsageRecord
from app.sandbox.opensandbox import OpenSandboxSandbox
from app.services.run_service import RunService
from test.support.fake_sandbox import ExecStep, FakeSandbox

# 喂给沙箱替身的 shell 命令原文（见模块 docstring：非 SQL）
CDM_ECHO_HELLO = "echo hello"
CDM_MULTI_LINE = "run-me"
CDM_ECHO_TICK = "echo tick"
CDM_ECHO_STILL_FINE = "echo still-fine"

TOOL_CALL_ID = "tool-call-1"
TOOL_CALL_ID_B = "tool-call-9"

SESSION = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"

FAKE_KEY_ENV = "LANE_TEST_SANDBOX_KEY"
"""单测专用的密钥环境变量名：值由 monkeypatch 注入（真密钥只进真实环境变量）。"""

FAKE_BAILIAN_API_URL = "https://ws-unit.cn-beijing.maas.aliyuncs.com/api/v1/agentstudio/sandbox"
"""百炼 E2B 接入完整地址的测试值（真实值由 .env 的 SANDBOX_BAILIAN_API_URL 提供）。"""

# ------------------------------------------------------------------ 假时钟与记录型 sink


@dataclass
class FakeClock:
    """可推进的 UTC 假时钟（patch `app.utils.clock.utc_now`，test-plan §3.6）。"""

    now: datetime = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    def utc_now(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class RecordingSinks:
    """记录型 sink：生命周期事件 / 输出块 / 计费分段（manager 的注入面）。"""

    lifecycle: list[SandboxLifecycleEvent] = field(default_factory=list)
    output: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    usage: list[SandboxUsageRecord] = field(default_factory=list)

    def on_lifecycle(self, event: SandboxLifecycleEvent) -> None:
        self.lifecycle.append(event)

    def on_output(self, session_id: str, chunk: SandboxOutputChunk) -> None:
        self.output.append((session_id, chunk.stream, chunk.data, chunk.exec_id))

    def on_usage(self, record: SandboxUsageRecord) -> None:
        self.usage.append(record)

    @property
    def phases(self) -> list[str]:
        return [event.phase for event in self.lifecycle]


# -------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """patch 模块级时钟：`app.sandbox.manager` 与 `app.sandbox.base` 都走 `clock.utc_now()`。"""
    clock = FakeClock()
    monkeypatch.setattr("app.utils.clock.utc_now", clock.utc_now)
    return clock


@pytest.fixture
def tmp_root(tmp_path: Path) -> Iterator[Path]:
    """替身工作根目录（tmp_path 自动清理，这里顺手显式 rmtree 保证无残留）。"""
    root = tmp_path / "sandbox-root"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sinks() -> RecordingSinks:
    return RecordingSinks()


@pytest.fixture
def created_sandboxes() -> list[FakeSandbox]:
    """factory 造出的替身实例清单（断言 create/kill 调用轨迹用）。"""
    return []


@pytest.fixture
def manager(
    tmp_root: Path, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> SandboxManager:
    """注入假 factory 的 manager：每会话一个 FakeSandbox，provider 由调用方给定。"""

    def fake_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            sandbox_id=f"fake-{provider}-{len(created_sandboxes) + 1}",
        )
        created_sandboxes.append(sandbox)
        return sandbox

    return SandboxManager(
        factory=fake_factory,
        idle_pause_seconds=60,
        on_lifecycle=sinks.on_lifecycle,
        on_output=sinks.on_output,
        on_usage_segment=sinks.on_usage,
    )


def as_fake(client: LcodeSandbox) -> FakeSandbox:
    """manager 返回的是协议类型；本文件的替身工厂保证它就是 FakeSandbox（运行期校验）。"""
    assert isinstance(client, FakeSandbox)
    return client


# --------------------------------------------------------------------- AC-002-1 相位机


def test_create_load_execute_phase_machine(manager: SandboxManager, sinks: RecordingSinks) -> None:
    """AC-002-1：create 相位机 none→creating→running；替身进注册表并已 provision。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))

    assert manager.phase(SESSION) == "running"
    assert manager.stable_phase(SESSION) == "running"
    assert manager.provider(SESSION) == "bailian"
    assert manager.get(SESSION) is sandbox
    assert manager.sessions() == (SESSION,)
    # 两条相位事件：creating（先）→ running（后），都带 provider 与 sandbox_id
    assert sinks.phases == ["creating", "running"]
    assert [event.provider for event in sinks.lifecycle] == ["bailian", "bailian"]
    assert sinks.lifecycle[0].sandbox_id is None  # creating 时尚无 id
    assert sinks.lifecycle[1].sandbox_id == sandbox.id
    assert sandbox.state.created is True

    # 命令真跑：替身默认回显命令（真子进程），输出块经 manager 转发到 sink
    result = manager.run_command(SESSION, CDM_ECHO_HELLO, exec_id=TOOL_CALL_ID)
    assert result.exit_code == 0
    assert "hello" in result.output
    assert (SESSION, "stdout", "hello\n", TOOL_CALL_ID) in sinks.output


def test_run_command_streams_chunks_with_exec_id(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """AC-002-1：一条命令多输出块按序到达，且每块带 exec_id（终端标注来源）。"""
    manager.create(SESSION, provider="bailian")
    sandbox = created_sandboxes[-1]
    sandbox.exec_script = [
        ExecStep(
            stdout_chunks=("line-1\n", "line-2\n"),
            stderr_chunks=("warn\n",),
            match="run-me",
        )
    ]

    result = manager.run_command(SESSION, CDM_MULTI_LINE, exec_id=TOOL_CALL_ID_B)

    assert result.exit_code == 0
    assert [chunk[2] for chunk in sinks.output] == ["line-1\n", "line-2\n", "warn\n"]
    assert {chunk[1] for chunk in sinks.output} == {"stdout", "stderr"}
    assert {chunk[3] for chunk in sinks.output} == {TOOL_CALL_ID_B}
    assert result.output == "line-1\nline-2\nwarn\n"


def test_run_command_requires_registered_sandbox(manager: SandboxManager) -> None:
    """未创建即执行 → 相位机错误（不静默造沙箱；调用方须先 create）。"""
    with pytest.raises(SandboxLifecycleError):
        manager.run_command(SESSION, CDM_ECHO_HELLO)


# ------------------------------------------------------------------- AC-002-2 空闲暂停


def test_idle_pause_fires_at_threshold(
    manager: SandboxManager, sinks: RecordingSinks, fake_clock: FakeClock
) -> None:
    """AC-002-2：空闲计时到阈值才显式 pause（假时钟推进）；phase=paused 事件可见。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    assert fake_clock.now == datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    fake_clock.advance(59)
    assert manager.evaluate_idle() == ()  # 未到阈值：不 pause
    assert manager.phase(SESSION) == "running"

    fake_clock.advance(1)  # 累计 60s = idle_pause_seconds
    assert manager.evaluate_idle() == (SESSION,)
    assert manager.phase(SESSION) == "paused"
    assert manager.stable_phase(SESSION) == "paused"
    assert sandbox.calls == ["create", "pause"]
    assert sinks.lifecycle[-1].phase == "paused"
    assert sinks.lifecycle[-1].reason == "idle"

    # 已 paused 的会话再评估不重复 pause（幂等）
    fake_clock.advance(120)
    assert manager.evaluate_idle() == ()
    assert sandbox.calls.count("pause") == 1


def test_idle_pause_skipped_while_run_active(
    manager: SandboxManager, fake_clock: FakeClock
) -> None:
    """AC-002-2 / Q3：run 活跃（begin_run..end_run）期间永不被空闲 pause；结束后恢复计时。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    manager.begin_run(SESSION)
    assert manager.active_runs(SESSION) == 1

    fake_clock.advance(3600)
    assert manager.evaluate_idle() == ()
    assert manager.phase(SESSION) == "running"
    assert "pause" not in sandbox.calls

    manager.end_run(SESSION)
    assert manager.active_runs(SESSION) == 0
    # end_run 重置计时：刚结束不算空闲
    assert manager.evaluate_idle() == ()
    fake_clock.advance(60)
    assert manager.evaluate_idle() == (SESSION,)


def test_activity_resets_idle_timer(manager: SandboxManager, fake_clock: FakeClock) -> None:
    """AC-002-2：命令执行（真实活动）也重置空闲计时——不是只在 create 计时。"""
    manager.create(SESSION, provider="bailian")
    fake_clock.advance(45)
    manager.run_command(SESSION, CDM_ECHO_TICK)
    fake_clock.advance(30)
    assert manager.evaluate_idle() == ()  # 距上次活动 30s < 60s
    fake_clock.advance(30)
    assert manager.evaluate_idle() == (SESSION,)


def test_resume_preserves_registry(
    manager: SandboxManager, sinks: RecordingSinks, fake_clock: FakeClock
) -> None:
    """AC-002-2：恢复沿用同一沙箱实例（不重建、不丢注册表），phase=resumed。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    fake_clock.advance(60)
    manager.evaluate_idle()
    assert manager.phase(SESSION) == "paused"

    resumed = manager.resume(SESSION)

    assert resumed is sandbox
    assert manager.get(SESSION) is sandbox
    assert manager.phase(SESSION) == "resumed"
    assert manager.stable_phase(SESSION) == "running"
    assert sandbox.state.paused is False
    assert sinks.lifecycle[-1].phase == "resumed"
    assert sinks.lifecycle[-1].reason == "user"


# ----------------------------------------------------------------- AC-015-4 计费分段


def test_usage_segments_running_and_paused(
    manager: SandboxManager, sinks: RecordingSinks, fake_clock: FakeClock
) -> None:
    """AC-015-4：running/paused 各一段、时长来自假时钟；进行中的段不落库（只有封口段进 sink）。"""
    sandbox = manager.create(SESSION, provider="bailian")
    assert sinks.usage == []  # 开段不推给 sink
    open_segment = manager.open_usage_segment(SESSION)
    assert open_segment is not None
    assert open_segment.phase == "running"
    assert open_segment.ended_at is None

    fake_clock.advance(90)
    manager.pause(SESSION, reason="manual")
    fake_clock.advance(30)
    manager.resume(SESSION)
    fake_clock.advance(10)
    manager.release(SESSION)

    segments = [item.segment for item in manager.usage_segments(SESSION)]
    assert [segment.phase for segment in segments] == ["running", "paused", "running"]
    assert [segment.duration_seconds for segment in segments] == [90, 30, 10]
    assert all(segment.ended_at is not None for segment in segments)  # release 封口最后一段
    assert manager.open_usage_segment(SESSION) is None

    # sink 收到的是带会话/provider/sandbox_id 的记录（sandbox_usage 表一行所需字段）
    assert [item.segment.phase for item in sinks.usage] == ["running", "paused", "running"]
    assert {item.session_id for item in sinks.usage} == {SESSION}
    assert {item.provider for item in sinks.usage} == {"bailian"}
    assert {item.sandbox_id for item in sinks.usage} == {sandbox.id}


def test_usage_segment_records_are_immutable_snapshots(
    manager: SandboxManager, fake_clock: FakeClock
) -> None:
    """分段是冻结快照：封口后改不动（防止上层拿到引用后误改历史分段）。"""
    manager.create(SESSION, provider="bailian")
    fake_clock.advance(5)
    manager.pause(SESSION, reason="manual")

    segment: SandboxUsageSegment = manager.usage_segments(SESSION)[0].segment

    with pytest.raises(FrozenInstanceError):
        segment.duration_seconds = 999  # type: ignore[misc]


# --------------------------------------------------------------------- AC-002-3 终止


def test_session_delete_kills_sandbox(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """AC-002-3：会话 DELETE → kill 沙箱 + phase=killed + 注册表移除。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    manager.release(SESSION)

    assert sandbox.calls == ["create", "kill"]
    assert manager.phase(SESSION) == "killed"
    assert manager.stable_phase(SESSION) == "killed"
    assert manager.get(SESSION) is None
    assert manager.sessions() == ()
    assert sinks.lifecycle[-1].phase == "killed"
    assert sinks.lifecycle[-1].reason == "user"

    # 幂等：重复 DELETE 不报错、不重复 kill
    manager.release(SESSION)
    assert sandbox.calls.count("kill") == 1


# ------------------------------------------- F5 释放顺序（kill 失败可观测且可重试）


class _RecordingDeleteDb:
    """`delete_session` 用的最小 AsyncSession 替身：只记录 purge 是否真的发生。"""

    def __init__(self, session_id: str) -> None:
        self.session_row = SimpleNamespace(id=uuid.UUID(session_id))
        self.deleted: list[object] = []
        self.flushes = 0

    async def scalar(self, statement: object) -> SimpleNamespace:
        return self.session_row

    async def scalars(self, statement: object) -> list[object]:
        return []

    async def get(self, model: object, key: object) -> SimpleNamespace:
        return self.session_row

    async def delete(self, row: object) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        self.flushes += 1


def _delete_service(manager: SandboxManager, db: _RecordingDeleteDb) -> RunService:
    return RunService(cast("Any", db), pipeline=cast("Any", None), manager=manager)


def test_release_kill_failure_closes_billing_segment(
    manager: SandboxManager,
    created_sandboxes: list[FakeSandbox],
    sinks: RecordingSinks,
    fake_clock: FakeClock,
) -> None:
    """F5-1：kill 抛 SandboxUnavailable 时计费段必须封口（不能悬空计费）。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    sandbox.set_kill_failure(True)
    fake_clock.advance(30)

    with pytest.raises(SandboxUnavailable):
        manager.release(SESSION)

    assert manager.open_usage_segment(SESSION) is None
    segments = manager.usage_segments(SESSION)
    assert len(segments) == 1
    assert segments[0].segment.ended_at is not None
    assert segments[0].segment.duration_seconds == 30
    assert sinks.usage[-1].segment.ended_at is not None
    assert manager.phase(SESSION) == "crashed"


def test_second_release_retries_kill_after_failure(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """F5-2：首次 kill 失败后，第二次 release 必须真的重试 kill（不能因摘表而 no-op）。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    sandbox.set_kill_failure(True)

    with pytest.raises(SandboxUnavailable):
        manager.release(SESSION)
    assert sandbox.calls.count("kill") == 1
    assert manager.get(SESSION) is None  # 死客户端不留在可服务注册表

    sandbox.set_kill_failure(False)
    manager.release(SESSION)

    assert sandbox.calls.count("kill") == 2
    assert sandbox.state.killed is True
    assert manager.phase(SESSION) == "killed"
    assert manager.get(SESSION) is None
    assert manager.sessions() == ()

    manager.release(SESSION)  # 成功后仍幂等
    assert sandbox.calls.count("kill") == 2


async def test_delete_session_does_not_purge_rows_when_kill_fails(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """F5-3：kill 失败时 delete_session 不得 purge 业务行（行没了沙箱还在烧钱）。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    sandbox.set_kill_failure(True)
    db = _RecordingDeleteDb(SESSION)

    with pytest.raises(SandboxUnavailable):
        await _delete_service(manager, db).delete_session(SESSION)

    assert db.deleted == []
    assert db.flushes == 0
    assert manager.get(SESSION) is None
    assert manager.phase(SESSION) == "crashed"


async def test_delete_session_retries_kill_then_purges(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """F5-4：重试 kill 成功后会话可正常删除（不是第二次直接跳过 kill 就删行）。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    sandbox.set_kill_failure(True)
    db = _RecordingDeleteDb(SESSION)
    service = _delete_service(manager, db)

    with pytest.raises(SandboxUnavailable):
        await service.delete_session(SESSION)
    assert db.deleted == []

    sandbox.set_kill_failure(False)
    await service.delete_session(SESSION)

    assert sandbox.calls.count("kill") == 2
    assert sandbox.state.killed is True
    assert manager.phase(SESSION) == "killed"
    assert db.deleted == [db.session_row]
    assert db.flushes == 1


def test_get_hides_crashed_client_after_release_failure(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """F5-5：释放失败后 get() 不得把 crash 客户端交给新 run（新 run 只能拉新实例）。"""
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    sandbox.set_kill_failure(True)

    with pytest.raises(SandboxUnavailable):
        manager.release(SESSION)

    assert manager.get(SESSION) is None
    assert SESSION not in manager.sessions()

    fresh = as_fake(manager.create(SESSION, provider="bailian"))
    assert fresh is not sandbox
    assert manager.get(SESSION) is fresh


# --------------------------------------------------------------------- AC-002-4 恢复


def test_unavailable_marks_crashed_and_rebuild_restores(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """AC-002-4：run_command 遇 SandboxUnavailable → crashed + 重建（recreated），provider 不变。"""
    sandbox = as_fake(manager.create(SESSION, provider="opensandbox"))
    sandbox.exec_script = [ExecStep.unavailable("剧本设定：沙箱被打散")]

    with pytest.raises(SandboxUnavailable):
        manager.run_command(SESSION, CDM_ECHO_HELLO)

    assert manager.phase(SESSION) == "crashed"
    assert manager.stable_phase(SESSION) == "crashed"
    assert "crashed" in sinks.phases
    assert sinks.lifecycle[-1].reason == "剧本设定：沙箱被打散"

    restored: list[str] = []
    result = manager.rebuild(SESSION, restore=lambda client: restored.append(client.id))

    assert len(created_sandboxes) == 2  # 重建确实换了实例
    assert result.sandbox is created_sandboxes[-1]
    assert result.restore_applied is True
    assert result.needs_reload is False
    assert restored == [result.sandbox.id]
    assert manager.phase(SESSION) == "recreated"
    assert manager.stable_phase(SESSION) == "running"  # 查询侧把 recreated 折叠成 running
    assert manager.provider(SESSION) == "opensandbox"  # provider 按会话固化
    assert result.sandbox.provider == "opensandbox"
    assert "recreated" in sinks.phases


def test_rebuild_without_restore_flags_needs_reload(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """AC-002-4：无恢复回调时如实标记 needs_reload（"明确告知需重克隆"，不静默丢弃）。"""
    as_fake(manager.create(SESSION, provider="bailian"))
    manager.mark_crashed(SESSION, reason="provider 报告不可用")

    result = manager.rebuild(SESSION)

    assert result.restore_applied is False
    assert result.needs_reload is True
    assert result.previous_sandbox_id == created_sandboxes[0].id


def test_probe_marks_crashed_when_provider_reports_unavailable(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """AC-002-4（被动路径）：provider status() 报不可用 → probe 标 crashed 并可重建。"""
    manager.create(SESSION, provider="bailian")
    sandbox = created_sandboxes[-1]
    sandbox.mark_unavailable("剧本设定：远端已回收")

    status = manager.probe(SESSION)

    assert status.available is False
    assert manager.phase(SESSION) == "crashed"
    result = manager.rebuild(SESSION)
    assert manager.phase(SESSION) == "recreated"
    assert result.sandbox is created_sandboxes[-1]


def test_crashed_sandbox_cannot_resume(manager: SandboxManager) -> None:
    """相位机：crashed 是终态（只能 rebuild），resume 必须报错而不是假装恢复。"""
    manager.create(SESSION, provider="bailian")
    manager.mark_crashed(SESSION, reason="剧本设定")
    with pytest.raises(SandboxLifecycleError):
        manager.resume(SESSION)


# --------------------------------------------------------------------- AC-002-6 切换


def test_factory_provider_switch_only_new_sessions(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-002-6：provider 固化在会话行上——改全局配置只影响新会话，旧会话不受影响。"""
    monkeypatch.setattr("app.core.config.sandbox_settings.provider", "bailian")
    manager.create(SESSION, provider=None)  # None = 用当前全局配置
    assert manager.provider(SESSION) == "bailian"

    monkeypatch.setattr("app.core.config.sandbox_settings.provider", "opensandbox")
    manager.create(SESSION_B, provider=None)

    assert manager.provider(SESSION) == "bailian"  # 旧会话保持固化值
    assert manager.provider(SESSION_B) == "opensandbox"
    assert [sandbox.provider for sandbox in created_sandboxes] == ["bailian", "opensandbox"]

    # 旧会话的既有实例不被替换（改配置不重建任何沙箱）
    assert manager.get(SESSION) is created_sandboxes[0]


def test_factory_rejects_unknown_provider() -> None:
    """AC-002-6 的结构性保证：注册表只认已实现 provider，未知值明确报错。"""
    unknown: str = "nonexistent"
    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox_factory.create_sandbox_client(unknown)
    message = str(excinfo.value)
    assert "nonexistent" in message
    assert "bailian" in message
    assert "opensandbox" in message


def test_factory_registry_exposes_both_providers() -> None:
    """两个 provider 各有一个实现类：新增 provider = 新增实现类 + 注册一项。"""
    assert set(sandbox_factory.PROVIDERS) == {"bailian", "opensandbox"}
    for provider, builder in sandbox_factory.PROVIDERS.items():
        assert callable(builder), provider


def test_factory_builds_provider_adapters() -> None:
    """工厂按 settings 造出对应适配器（不产生网络调用：只构造对象）。"""
    cfg = _cfg()
    bailian = sandbox_factory.create_sandbox_client("bailian", session_id=SESSION, settings=cfg)
    opensandbox = sandbox_factory.create_sandbox_client(
        "opensandbox", session_id=SESSION, settings=cfg
    )
    assert isinstance(bailian, BailianSandbox) and bailian.provider == "bailian"
    assert isinstance(opensandbox, OpenSandboxSandbox) and opensandbox.provider == "opensandbox"
    assert bailian.session_id == SESSION


def test_manager_unknown_provider_raises_configuration_error() -> None:
    """manager 侧同样把未知 provider 转成配置错误（不落进相位机）。"""
    manager = SandboxManager(factory=sandbox_factory.create_sandbox_client, idle_pause_seconds=60)
    unknown: SandboxProvider = cast("SandboxProvider", "nonexistent")  # 运行期才会被拒
    with pytest.raises(SandboxConfigurationError) as excinfo:
        manager.create(SESSION, provider=unknown)
    assert "nonexistent" in str(excinfo.value)


# ----------------------------------------------------------------------- 关停与相位机


def test_shutdown_pauses_running_sandboxes(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """U9：服务关停对 running 沙箱显式 pause（保状态可续），不 kill。"""
    manager.create(SESSION, provider="bailian")
    manager.create(SESSION_B, provider="opensandbox")

    manager.shutdown()

    assert manager.phase(SESSION) == "paused"
    assert manager.phase(SESSION_B) == "paused"
    assert all("kill" not in sandbox.calls for sandbox in created_sandboxes)
    assert [event.reason for event in sinks.lifecycle if event.phase == "paused"] == [
        "shutdown",
        "shutdown",
    ]
    # 关停幂等：重复调用不再产生 pause
    call_counts = [len(sandbox.calls) for sandbox in created_sandboxes]
    manager.shutdown()
    assert [len(sandbox.calls) for sandbox in created_sandboxes] == call_counts


def test_illegal_transition_raises(manager: SandboxManager) -> None:
    """相位机约束：重复 create / 未注册 resume / running 未 pause 就 resume 都报错。"""
    manager.create(SESSION, provider="bailian")

    with pytest.raises(SandboxLifecycleError):
        manager.create(SESSION, provider="bailian")  # 已存在
    with pytest.raises(SandboxLifecycleError):
        manager.resume(SESSION)  # running 未 pause
    with pytest.raises(SandboxLifecycleError):
        manager.resume(SESSION_B)  # 未注册
    with pytest.raises(SandboxLifecycleError):
        manager.pause(SESSION_B)  # 未注册


def test_create_failure_marks_crashed_not_silently_none(
    sinks: RecordingSinks, tmp_root: Path
) -> None:
    """create 失败（凭证/配额/网络）→ 相位落 crashed，注册表不留半成品。"""

    def failing_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        return FakeSandbox(root=tmp_root, provider=provider, fail_create=True)

    manager = SandboxManager(
        factory=failing_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    with pytest.raises(SandboxUnavailable):
        manager.create(SESSION, provider="bailian")

    assert manager.phase(SESSION) == "crashed"
    assert manager.stable_phase(SESSION) == "crashed"
    assert manager.get(SESSION) is None
    assert sinks.phases == ["creating", "crashed"]


def test_output_handler_error_does_not_break_command(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """输出回调抛错只记日志：命令结果照常返回（回调链是旁路，不是主链路）。"""

    def exploding_handler(chunk: SandboxOutputChunk) -> None:
        raise RuntimeError("sink 坏了")

    sandbox = manager.create(SESSION, provider="bailian")
    sandbox.add_output_handler(exploding_handler)

    result = manager.run_command(SESSION, CDM_ECHO_STILL_FINE)

    assert result.exit_code == 0
    assert "still-fine" in result.output


def test_status_projection_for_unregistered_session(manager: SandboxManager) -> None:
    """查询侧投影：未注册会话返回 none（GET /sessions/{id}/sandbox 的 none 语义）。"""
    status = manager.status(SESSION)

    assert status.phase == "none"
    assert status.sandbox_id is None
    assert status.available is False
    assert manager.stable_phase(SESSION) == "none"
    assert manager.provider(SESSION) is None


# =============================================================== 适配器薄翻译（注入式单测）
#
# 真百炼 / 真 OpenSandbox 冒烟单列 `integration/test_sandbox_real.py`（requires_* 标记）；
# 这里用注入的假 SDK 句柄验证**翻译层本身**：凭证读取、参数拼装、结果映射、异常翻译、
# 部分成功语义——不连任何远端。


def _cfg(**overrides: str | int | float) -> SandboxSettings:
    """测试配置快照：全部显式给值，不依赖 .env/.env.test 的真实内容。

    覆盖用 `model_copy(update=…)`（跳过二次校验）：这里的值都是本文件内受控的字面量。
    """
    settings = SandboxSettings(
        provider="bailian",
        idle_pause_seconds=60,
        bailian_api_key_env=FAKE_KEY_ENV,
        bailian_template="",
        bailian_api_url=FAKE_BAILIAN_API_URL,
        bailian_aliyun_uid="",
        opensandbox_endpoint="os.example.test:8080",
        opensandbox_api_key_env=FAKE_KEY_ENV,
        opensandbox_image="",
    )
    if overrides:
        return settings.model_copy(update=dict(overrides))
    return settings


class _FakeE2BHandle:
    """e2b Sandbox 句柄替身：commands.run（含输出回调）/ files / 生命周期 / get_info。"""

    def __init__(
        self,
        *,
        sandbox_id: str = "e2b-42",
        stdout_chunks: tuple[str, ...] = ("out\n",),
        stderr_chunks: tuple[str, ...] = (),
        exit_code: int = 0,
        state: str = "running",
        fail: dict[str, Exception] | None = None,
        files: dict[str, bytes] | None = None,
        fail_write_paths: tuple[str, ...] = (),
    ) -> None:
        self.sandbox_id = sandbox_id
        self.stdout_chunks = stdout_chunks
        self.stderr_chunks = stderr_chunks
        self.exit_code = exit_code
        self.state = state
        self.fail = fail or {}
        self.stored_files: dict[str, bytes] = dict(files or {})
        self.fail_write_paths = fail_write_paths
        self.calls: list[str] = []
        self.commands = _FakeE2BCommands(self)
        self.files = _FakeE2BFiles(self)  # e2b 语义：handle.files.write/read

    def pause(self) -> None:
        self.calls.append("pause")
        if "pause" in self.fail:
            raise self.fail["pause"]

    def connect(self) -> None:
        self.calls.append("connect")

    def kill(self) -> None:
        self.calls.append("kill")

    def get_info(self) -> SimpleNamespace:
        return SimpleNamespace(state=self.state)


class _FakeE2BCommands:
    def __init__(self, handle: _FakeE2BHandle) -> None:
        self._handle = handle

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self._handle.calls.append("run")
        if "run" in self._handle.fail:
            raise self._handle.fail["run"]
        # 模拟 e2b 语义：on_stdout/on_stderr 在执行过程中逐块回调
        on_stdout = kwargs.get("on_stdout")
        on_stderr = kwargs.get("on_stderr")
        for chunk in self._handle.stdout_chunks:
            if callable(on_stdout):
                on_stdout(chunk)
        for chunk in self._handle.stderr_chunks:
            if callable(on_stderr):
                on_stderr(chunk)
        return SimpleNamespace(
            stdout="".join(self._handle.stdout_chunks),
            stderr="".join(self._handle.stderr_chunks),
            exit_code=self._handle.exit_code,
        )


class _FakeE2BFiles:
    def __init__(self, handle: _FakeE2BHandle) -> None:
        self._handle = handle

    def write(self, path: str, data: bytes) -> None:
        if path in self._handle.fail_write_paths:
            raise FileNotFoundError(f"no such file or directory (path): {path}")
        self._handle.stored_files[path] = bytes(data)

    def read(self, path: str, format: str = "text") -> bytes:
        if path not in self._handle.stored_files:
            raise FileNotFoundError(f"no such file or directory (path): {path}")
        return self._handle.stored_files[path]


class _FakeOSHandle:
    """opensandbox SandboxSync 句柄替身：commands.run / files / 生命周期 / get_info。"""

    def __init__(
        self,
        *,
        sandbox_id: str = "os-7",
        execution_text: str = "hi\n",
        stderr_text: str = "",
        exit_code: int | None = 0,
        state: str = "RUNNING",
        fail: dict[str, Exception] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.id = sandbox_id
        self.execution_text = execution_text
        self.stderr_text = stderr_text
        self.exit_code = exit_code
        self.state = state
        self.fail = fail or {}
        self.stored_files: dict[str, bytes] = dict(files or {})
        self.calls: list[str] = []
        self.run_kwargs: dict[str, object] = {}
        self.commands = _FakeOSCommands(self)
        self.files = _FakeOSFiles(self)  # opensandbox 语义：handle.files.write_file/read_bytes

    def pause(self) -> None:
        self.calls.append("pause")

    def kill(self) -> None:
        self.calls.append("kill")

    def get_info(self) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(state=self.state))


class _FakeOSCommands:
    def __init__(self, handle: _FakeOSHandle) -> None:
        self._handle = handle

    def run(self, cmd: str, *, opts: object = None, handlers: object = None) -> SimpleNamespace:
        self._handle.calls.append("run")
        self._handle.run_kwargs = {"opts": opts, "handlers": handlers}
        if "run" in self._handle.fail:
            raise self._handle.fail["run"]
        # 模拟 SDK 的 SSE 回调：逐块喂给 on_stdout/on_stderr（handler 是 OutputMessage）
        stream = SimpleNamespace(
            on_stdout=getattr(handlers, "on_stdout", None),
            on_stderr=getattr(handlers, "on_stderr", None),
        )
        for chunk in (self._handle.execution_text,):
            if stream.on_stdout is not None:
                stream.on_stdout(SimpleNamespace(text=chunk))
        if self._handle.stderr_text and stream.on_stderr is not None:
            stream.on_stderr(SimpleNamespace(text=self._handle.stderr_text))
        return SimpleNamespace(
            text=self._handle.execution_text,
            logs=SimpleNamespace(stderr=[SimpleNamespace(text=self._handle.stderr_text)]),
            exit_code=self._handle.exit_code,
            error=None,
        )


class _FakeOSFiles:
    def __init__(self, handle: _FakeOSHandle) -> None:
        self._handle = handle

    def write_file(self, path: str, data: bytes) -> None:
        if "write" in self._handle.fail:
            raise self._handle.fail["write"]
        self._handle.stored_files[path] = bytes(data)

    def read_bytes(self, path: str) -> bytes:
        if path not in self._handle.stored_files:
            raise FileNotFoundError(f"no such file: {path}")
        return self._handle.stored_files[path]


def _bailian(handle: _FakeE2BHandle, **cfg_overrides: str | int | float) -> BailianSandbox:
    def factory(**kwargs: object) -> _FakeE2BHandle:
        return handle

    return BailianSandbox(
        settings=_cfg(**cfg_overrides),
        sandbox_factory=factory,
    )


def _opensandbox(
    handle: _FakeOSHandle,
    *,
    resume_factory: Callable[..., Any] | None = None,
    image: str = "python:3.11",
    **cfg_overrides: str | int | float,
) -> OpenSandboxSandbox:
    return OpenSandboxSandbox(
        settings=_cfg(**cfg_overrides),
        image=image,
        sandbox_factory=lambda *args, **kwargs: handle,
        resume_factory=resume_factory,
    )


# ---- 百炼（E2B 协议）----


def test_bailian_missing_api_key_error_names_only_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺凭证：消息只含环境变量名（NFR-004——错误面不出现密钥值）。"""
    monkeypatch.delenv(FAKE_KEY_ENV, raising=False)
    sandbox = _bailian(_FakeE2BHandle())

    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.create()

    assert FAKE_KEY_ENV in str(excinfo.value)
    assert "密钥" in str(excinfo.value) or "凭证" in str(excinfo.value)


def test_bailian_create_reads_key_from_env_and_disables_client_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create 走官方 E2B 接入口径：完整 api_url + Authorization Bearer，api_key 仅格式占位。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> _FakeE2BHandle:
        captured.update(kwargs)
        return _FakeE2BHandle(sandbox_id="e2b-99")

    sandbox = BailianSandbox(
        settings=_cfg(
            bailian_template="tmpl-x",
            bailian_api_url="https://ws-x.cn-beijing.maas.aliyuncs.com/api/v1/agentstudio/sandbox",
        ),
        sandbox_factory=factory,
    )
    sandbox.create(timeout_seconds=120)

    assert captured["api_url"] == (
        "https://ws-x.cn-beijing.maas.aliyuncs.com/api/v1/agentstudio/sandbox"
    )
    # 百炼不做 api_key 业务鉴权：uid 缺失时用满足 e2b_<hex> 格式的占位符
    assert captured["api_key"] == "e2b_0"
    # 真实鉴权走 Authorization 头（密钥只从 *_API_KEY_ENV 读）
    assert captured["headers"] == {"Authorization": "Bearer unit-test-fake-key"}
    # e2b 会拼成 https://api.<domain>，百炼接入禁止传 domain
    assert "domain" not in captured
    assert captured["validate_api_key"] is False
    assert captured["template"] == "tmpl-x"
    assert captured["timeout"] == 120
    assert sandbox.id == "e2b-99"


def test_bailian_create_uses_aliyun_uid_in_placeholder_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了 aliyun uid 时，api_key 拼成官方示例的 `e2b_<uid>` 形态（仍非真实密钥）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> _FakeE2BHandle:
        captured.update(kwargs)
        return _FakeE2BHandle(sandbox_id="e2b-100")

    sandbox = BailianSandbox(
        settings=_cfg(bailian_aliyun_uid="123456"),
        sandbox_factory=factory,
    )
    sandbox.create()

    assert captured["api_key"] == "e2b_123456"
    assert captured["headers"] == {"Authorization": "Bearer unit-test-fake-key"}
    # api_url 已配置时必须传给 SDK（空值由 fail-fast 用例单独覆盖，不再静默省略）
    assert captured["api_url"] == FAKE_BAILIAN_API_URL


def test_bailian_create_fails_fast_when_api_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api_url 缺失即配置错误：不得静默落到 e2b 默认公网端点（否则误报「鉴权被拒」）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    sandbox = _bailian(_FakeE2BHandle(), bailian_api_url="")

    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.create()

    message = str(excinfo.value)
    assert "SANDBOX_BAILIAN_API_URL" in message
    assert "unit-test-fake-key" not in message


def test_bailian_misconfigured_key_env_never_echoes_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`*_API_KEY_ENV` 误填密钥本体：错误消息只给占位文本，不回显原值（NFR-004）。"""
    misconfigured = "sk-unit-test-secret-value"
    monkeypatch.delenv(misconfigured, raising=False)
    sandbox = _bailian(_FakeE2BHandle(), bailian_api_key_env=misconfigured)

    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.create()

    message = str(excinfo.value)
    assert misconfigured not in message
    assert "<非法的环境变量名" in message


def test_bailian_auth_failure_message_never_echoes_misconfigured_key_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鉴权失败消息同样只渲染合法变量名（同一泄漏路径的另一处插值）。"""
    misconfigured = "sk-unit-test-secret-value"
    monkeypatch.setenv(misconfigured, "unit-test-fake-key")  # 让 create 走到执行期
    sandbox = _bailian(
        _FakeE2BHandle(fail={"run": AuthenticationException("auth denied")}),
        bailian_api_key_env=misconfigured,
    )
    sandbox.create()

    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.run_command("ls")

    message = str(excinfo.value)
    assert misconfigured not in message
    assert "<非法的环境变量名" in message


def test_bailian_execute_maps_result_and_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行结果映射 + stdout/stderr 分块带 exec_id（AC-002-1 的输出流源头）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(
        stdout_chunks=("out-1\n", "out-2\n"), stderr_chunks=("err-1\n",), exit_code=3
    )
    sandbox = _bailian(handle)
    sandbox.create()
    chunks: list[SandboxOutputChunk] = []
    sandbox.add_output_handler(chunks.append)

    result = sandbox.run_command("ls -la", exec_id=TOOL_CALL_ID)

    assert result.exit_code == 3
    assert result.output == "out-1\nout-2\nerr-1\n"
    assert [(chunk.stream, chunk.data, chunk.exec_id) for chunk in chunks] == [
        ("stdout", "out-1\n", TOOL_CALL_ID),
        ("stdout", "out-2\n", TOOL_CALL_ID),
        ("stderr", "err-1\n", TOOL_CALL_ID),
    ]
    assert handle.calls == ["run"]


def test_bailian_sdk_failure_translates_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 故障 → SandboxUnavailable（消息只含类型名；AC-002-4 的输入）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(fail={"run": RuntimeError("connection reset")})
    sandbox = _bailian(handle)
    sandbox.create()

    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run_command("ls")

    assert "RuntimeError" in str(excinfo.value)
    assert excinfo.value.detail  # 明细进 detail（不进事件/响应），供排查


def test_bailian_nonzero_exit_is_result_not_sandbox_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e2b 的非零退出异常是**命令结果**（如非 git 目录 exit 128），不是沙箱故障。

    `CommandExitException` 继承 `CommandResult`（带 stdout/stderr/exit_code），
    适配器必须把它映射回 `ExecuteResponse`，否则 `is_git_repository()` 的
    `exit_code != 0` 分支永远走不到（round-0 基线判据被破坏）。
    """
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(
        fail={
            "run": CommandExitException(
                stdout="",
                stderr="fatal: not a git repository",
                exit_code=128,
                error=None,
            )
        }
    )
    sandbox = _bailian(handle)
    sandbox.create()
    chunks: list[SandboxOutputChunk] = []
    sandbox.add_output_handler(chunks.append)

    result = sandbox.run_command("git rev-parse --is-inside-work-tree")

    assert result.exit_code == 128
    assert result.output == "fatal: not a git repository"  # combine_output：stdout → stderr
    assert result.truncated is False
    # 流式块已在 run 迭代期间实时发过；异常路径不得再从 exc 字段造块重复 emit
    assert chunks == []


def test_is_git_repository_false_when_probe_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """round-0 判据：非 git 目录下探针 exit 128 → False（而不是抛 SandboxUnavailable）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(
        fail={
            "run": CommandExitException(
                stdout="",
                stderr="fatal: not a git repository",
                exit_code=128,
                error=None,
            )
        }
    )
    sandbox = _bailian(handle)
    sandbox.create()

    assert sandbox.is_git_repository() is False


def test_is_git_repository_true_when_probe_reports_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """round-0 判据：探针 exit 0 且输出 true → True。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    sandbox = _bailian(_FakeE2BHandle(stdout_chunks=("true\n",), exit_code=0))
    sandbox.create()

    assert sandbox.is_git_repository() is True


def test_bailian_auth_failure_translates_to_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鉴权被拒 → SandboxConfigurationError（可修配置，不进重建路径）。"""
    from e2b import AuthenticationException

    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(fail={"run": AuthenticationException("401: Unauthorized")})
    sandbox = _bailian(handle)
    sandbox.create()

    with pytest.raises(SandboxConfigurationError):
        sandbox.run_command("ls")


def test_bailian_upload_download_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量上传/下载逐文件部分成功：坏文件给 error，好文件照常（deepagents 契约）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(files={"a.txt": b"A"}, fail_write_paths=("bad/x.txt",))
    sandbox = _bailian(handle)
    sandbox.create()

    uploads = sandbox.upload_files([("a.txt", b"A2"), ("bad/x.txt", b"B")])
    assert uploads[0].error is None
    assert uploads[1].error == "file_not_found"  # FileNotFoundError 归一化
    assert handle.stored_files["a.txt"] == b"A2"

    downloads = sandbox.download_files(["a.txt", "missing.txt"])
    assert downloads[0].content == b"A2"  # 上传覆盖写（e2b 语义：同名即覆盖）
    assert downloads[1].content is None
    assert downloads[1].error == "file_not_found"


def test_bailian_status_maps_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """status 只读探测：running/paused/未知/未 provision 都结构化回报（不抛错）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(state="running")
    sandbox = _bailian(handle)
    assert sandbox.status().phase == "none"  # 未 provision

    sandbox.create()
    assert sandbox.status().phase == "running"
    assert sandbox.status().available is True

    handle.state = "paused"
    assert sandbox.status().phase == "paused"

    handle.state = "weird"
    weird = sandbox.status()
    assert weird.available is False and weird.phase == "crashed"


def test_bailian_lifecycle_calls_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """pause→resume→kill 的 SDK 调用次序（resume 走 connect：e2b 无独立 resume）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle()
    sandbox = _bailian(handle)
    sandbox.create()
    sandbox.pause()
    sandbox.resume()
    sandbox.kill()
    assert handle.calls == ["pause", "connect", "kill"]


# ---- OpenSandbox（自有协议）----


def test_opensandbox_requires_image_configuration() -> None:
    """镜像缺配置即明确报错（不硬编码镜像；配置项 SANDBOX_OPENSANDBOX_IMAGE 为空时 fail-fast）。"""
    sandbox = OpenSandboxSandbox(settings=_cfg(), image="")
    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.create()
    assert "SANDBOX_OPENSANDBOX_IMAGE" in str(excinfo.value)


def test_opensandbox_uses_settings_image_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式传 image 时取 settings.opensandbox_image（去空白），作为 SDK 首个位置参数。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> _FakeOSHandle:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeOSHandle(sandbox_id="os-settings")

    sandbox = OpenSandboxSandbox(
        settings=_cfg(opensandbox_image="  python:3.12-slim  "),
        sandbox_factory=factory,
    )
    sandbox.create()

    assert captured["args"][0] == "python:3.12-slim"
    assert sandbox.id == "os-settings"


def test_opensandbox_constructor_image_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式构造参数优先于 settings 镜像（同样去空白）；真实 SDK 调用用假 factory 截获。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> _FakeOSHandle:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeOSHandle(sandbox_id="os-override")

    sandbox = OpenSandboxSandbox(
        settings=_cfg(opensandbox_image="settings:image"),
        image="  constructor:image  ",
        sandbox_factory=factory,
    )
    sandbox.create()

    assert captured["args"][0] == "constructor:image"
    assert sandbox.id == "os-override"


def test_opensandbox_misconfigured_key_env_never_echoes_value() -> None:
    """同一泄漏路径（OpenSandbox 侧）：误填密钥本体时消息只给占位文本（NFR-004）。"""
    misconfigured = "sk-unit-test-secret-value"
    sandbox = _opensandbox(_FakeOSHandle(), opensandbox_api_key_env=misconfigured)

    with pytest.raises(SandboxConfigurationError) as excinfo:
        sandbox.create()

    message = str(excinfo.value)
    assert misconfigured not in message
    assert "<非法的环境变量名" in message


def test_opensandbox_execute_maps_execution_and_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution → ExecuteResponse 映射 + SSE 回调块带 exec_id（U4 的流式接线）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeOSHandle(execution_text="hi\n", stderr_text="warn\n", exit_code=7)
    sandbox = _opensandbox(handle)
    sandbox.create()
    chunks: list[SandboxOutputChunk] = []
    sandbox.add_output_handler(chunks.append)

    result = sandbox.run_command("python x.py", exec_id=TOOL_CALL_ID_B)

    assert result.exit_code == 7
    assert result.output == "hi\nwarn\n"
    assert [(chunk.stream, chunk.data, chunk.exec_id) for chunk in chunks] == [
        ("stdout", "hi\n", TOOL_CALL_ID_B),
        ("stderr", "warn\n", TOOL_CALL_ID_B),
    ]
    assert "run" in handle.calls


def test_opensandbox_resume_swaps_handle_via_classmethod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resume：SDK 是类方法且端点可能变 → 换新句柄（沿用同一 sandbox_id）。

    句柄已换的判据走**可观测行为**：resume 前把新句柄状态设为 PAUSED，resume 后
    `status()` 读到的是新句柄的 get_info。
    """
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    old = _FakeOSHandle(sandbox_id="os-7")
    new = _FakeOSHandle(sandbox_id="os-7", state="PAUSED")
    captured: dict[str, object] = {}

    def resume_factory(sandbox_id: str, **kwargs: object) -> _FakeOSHandle:
        captured.update({"sandbox_id": sandbox_id, **kwargs})
        return new

    sandbox = _opensandbox(old, resume_factory=resume_factory)
    sandbox.create()
    sandbox.pause()  # 旧句柄只记调用，状态仍是 RUNNING
    assert sandbox.status().phase == "running"  # resume 前：读到的是旧句柄
    sandbox.resume()

    assert captured["sandbox_id"] == "os-7"
    assert captured["connection_config"] is not None
    assert sandbox.id == "os-7"
    assert sandbox.status().phase == "paused"  # resume 后：读到的是新句柄（PAUSED）


def test_opensandbox_connection_failure_translates_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensandbox.exceptions import SandboxConnectionException

    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeOSHandle(fail={"run": SandboxConnectionException("refused")})
    sandbox = _opensandbox(handle)
    sandbox.create()

    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run_command("ls")

    assert "SandboxConnectionException" in str(excinfo.value)
    assert "不可达或未就绪" in str(excinfo.value)


def test_opensandbox_status_maps_provider_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeOSHandle(state="RUNNING")
    sandbox = _opensandbox(handle)
    sandbox.create()

    assert sandbox.status().phase == "running"
    handle.state = "PAUSED"
    assert sandbox.status().phase == "paused"
    handle.state = "TERMINATED"
    terminated = sandbox.status()
    assert terminated.available is False and terminated.phase == "crashed"


def test_opensandbox_upload_download_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeOSHandle(files={"a.txt": b"A"})
    sandbox = _opensandbox(handle)
    sandbox.create()

    uploads = sandbox.upload_files([("a.txt", b"A2")])
    assert uploads[0].error is None
    assert handle.stored_files["a.txt"] == b"A2"

    downloads = sandbox.download_files(["a.txt", "missing.txt"])
    assert downloads[0].content == b"A2"  # 上传覆盖写
    assert downloads[1].error == "file_not_found"
