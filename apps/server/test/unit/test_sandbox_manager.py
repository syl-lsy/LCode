"""unit 层：SandboxManager 相位机 / 空闲 pause / 重建 / 计费分段 + 适配器薄翻译。

对应 test-plan-m2 §2.2 的 unit 列（T3）与 §2.6 的 AC-015-4：

| 用例 | AC |
|---|---|
| `test_create_load_execute_phase_machine` | AC-002-1（create→running、输出块回调、registry） |
| `test_run_command_streams_chunks_with_exec_id` | AC-002-1（stdout 增量 + exec_id 标注） |
| `test_idle_pause_fires_at_threshold` | AC-002-2（假时钟推进到阈值才 pause） |
| `test_idle_pause_skipped_while_run_active` | AC-002-2（Q3：run 活跃期间永不 pause） |
| `test_ensure_reuse_refreshes_idle_against_sweeper` | Q3（评审项 2：ensure→begin_run 窗口不被空闲 pause） |
| `test_resume_preserves_registry` | AC-002-2（恢复沿用同一实例） |
| `test_usage_segments_running_and_paused` | AC-015-4（running/paused 分段 + 时长） |
| `test_session_delete_kills_sandbox` | AC-002-3（DELETE → kill） |
| `test_unavailable_marks_crashed_and_rebuild_restores` | AC-002-4（不可用→crashed→recreated） |
| `test_probe_marks_crashed_when_provider_reports_unavailable` | AC-002-4（被动探测路径） |
| `test_factory_provider_switch_only_new_sessions` | AC-002-6（provider 按会话固化） |
| `test_shutdown_pauses_running_sandboxes` | U9（关停 pause 而非 kill） |
| `test_illegal_transition_raises` | 相位机约束（设计 §4.3） |
| `test_concurrent_ensure_creates_once` | S2（并发 ensure 同一会话只 create 一次） |
| `test_ensure_reuses_registered_instance` | S2（第二次 ensure 复用实例，不再 create） |
| `test_ensure_resumes_paused_session` | S2（paused 会话 ensure 走 resume） |
| `test_ensure_recreates_from_terminal_phases` | S2（killed/crashed 后 ensure 拉新实例） |
| `test_ensure_different_sessions_do_not_block` | S2（分片锁：不同会话互不阻塞） |
| `test_run_service_ensure_backend_offloads_to_thread` | S1（run 层 ensure 经 to_thread 卸载） |
| `test_ensure_adopts_persisted_sandbox` | S3（不在册 + 持久 id → adopt 重连，不 create） |
| `test_ensure_adopt_failure_falls_back_to_rebuild` | S3（adopt 失败 → rebuild + recreated + needs_reload） |
| `test_ensure_prefers_registered_instance_over_persisted_id` | S3（决策矩阵：在册实例优先于持久 id） |
| `test_bailian_adopt_*` / `test_opensandbox_adopt_*` | S3（适配器 adopt 的注入式单测） |
| 适配器段（`test_bailian_*` / `test_opensandbox_*`） | 双适配器薄翻译的注入式单测（真 SDK 冒烟单列 requires_sandbox） |

时钟固定（test-plan §3.6）：本模块用 `FakeClock` patch `app.utils.clock.utc_now`，
空闲 pause 靠 `advance()` 推进；分段时长来自假时钟而非真墙钟。

命名说明：manager 的命令入口叫 `run_command`（`execute` 留给 deepagents 协议成员——
`LcodeSandbox.execute` 只是它的薄委托）；下方 `CDM_*` 常量是**喂给沙箱替身执行的
shell 命令原文**（替身在工作根目录真跑子进程），不是任何数据库语句。
"""

from __future__ import annotations

import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter, sleep
from types import SimpleNamespace
from typing import Any, cast

import pytest
from e2b import AuthenticationException, SandboxNotFoundException
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
from app.services.message_service import RunLaunch
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


def test_ensure_reuse_refreshes_idle_against_sweeper(
    manager: SandboxManager,
    created_sandboxes: list[FakeSandbox],
    fake_clock: FakeClock,
) -> None:
    """Q3（评审项 2）：ensure 复用刚到空闲阈值的实例必须刷新活跃计时——sweeper 在
    「ensure 返回 → begin_run」窗口内不得 pause 刚就绪的 run。

    确定性构造：fake_clock 推到阈值（最坏输入）→ ensure 走复用路径 → **串行**执行与
    sweeper 线程完全相同的决策代码 `evaluate_idle`——它锁内读到的字段与任何并发时序下
    完全一致：ensure 在锁内刷新过计时则 idle=0 不 pause；若刷新缺失（回归）则必然
    pause，本用例红。run_service 的调用序正是 ensure → (窗口) → begin_run。
    """
    sandbox = as_fake(manager.create(SESSION, provider="bailian"))
    fake_clock.advance(60)  # 恰到 idle_pause_seconds 阈值：sweeper 随时可合法 pause

    client, _needs = manager.ensure(SESSION, provider="bailian")  # 复用路径（begin_run 未执行）
    assert manager.evaluate_idle() == ()  # 竞态窗口内的 sweeper 决策
    manager.begin_run(SESSION)

    assert client is sandbox
    assert manager.active_runs(SESSION) == 1
    assert manager.stable_phase(SESSION) == "running"
    assert "pause" not in sandbox.calls  # run 就绪到活跃之间不允许被 pause


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


# ============================================================ S1/S2 ensure 与会话级锁
#
# S1：run_service 把同步 provision 经 asyncio.to_thread 卸到工作线程 → manager 必须
# 自带线程安全。S2 的核心不变量：**同一会话的「检查-决策-登记」在同一把会话锁内原子
# 完成**（并发 ensure 至多 provision 一次），不同会话各持一把锁互不阻塞。


class _SlowCreateSandbox(FakeSandbox):
    """create 可延迟/可设闸的替身（S2 并发测试：拉长「检查-创建-登记」窗口供竞争）。"""

    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        create_gate: threading.Event | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._delay_seconds = delay_seconds
        self.create_gate = create_gate
        self.enter_create = threading.Event()
        """进入 create 时置位（证明 A 已在 provision 中，供 B 的无阻塞断言锚定时序）。"""

    def create(self, *, timeout_seconds: int | None = None) -> None:
        self.enter_create.set()
        if self.create_gate is not None:
            self.create_gate.wait(timeout=10)
        if self._delay_seconds:
            sleep(self._delay_seconds)
        super().create(timeout_seconds=timeout_seconds)


def test_concurrent_ensure_creates_once(tmp_root: Path, sinks: RecordingSinks) -> None:
    """S2 核心不变量：多线程同时 ensure 同一会话，工厂只被调用一次。

    Barrier 同步起点 + create 延迟把竞争窗口拉满——若无会话锁，8 个线程都会穿过
    「查注册表」在窗口内各自 create；有锁时后到者在锁上等待并复用先到者的实例。
    """
    created: list[FakeSandbox] = []
    barrier = threading.Barrier(8)

    def counting_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = _SlowCreateSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            sandbox_id=f"fake-{provider}-{len(created) + 1}",
            delay_seconds=0.03,
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=counting_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    results: list[LcodeSandbox] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            client, _needs = manager.ensure(SESSION, provider="bailian")
            results.append(client)
        except Exception as exc:  # noqa: BLE001 —— 线程内异常收集到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, name=f"ensure-{n}") for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads), "ensure 线程未在超时内收敛"
    assert errors == []

    assert len(created) == 1, f"并发 ensure 触发了 {len(created)} 次 provision（锁失效）"
    assert len({id(client) for client in results}) == 1  # 全部线程拿到同一实例
    assert results[0] is created[0]
    assert manager.phase(SESSION) == "running"
    assert as_fake(results[0]).calls == ["create"]
    assert sinks.phases == ["creating", "running"]  # 相位事件也恰好一对


def test_ensure_reuses_registered_instance(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """S2：第二次 ensure 直接复用在册实例（不 create、不 resume）。"""
    first, needs_first = manager.ensure(SESSION, provider="bailian")
    second, needs_second = manager.ensure(SESSION, provider="bailian")

    assert needs_first is False and needs_second is False
    assert first is second is created_sandboxes[0]
    assert len(created_sandboxes) == 1
    assert as_fake(first).calls == ["create"]


def test_ensure_resumes_paused_session(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """S2：paused 会话 ensure 走 resume（沿用同一实例），而不是 create 拉新。"""
    sandbox = as_fake(manager.ensure(SESSION, provider="bailian")[0])
    manager.pause(SESSION, reason="manual")
    assert manager.stable_phase(SESSION) == "paused"

    resumed, needs_reload = manager.ensure(SESSION, provider="bailian")

    assert needs_reload is False
    assert resumed is sandbox is created_sandboxes[0]
    assert len(created_sandboxes) == 1  # 没有新实例
    assert manager.stable_phase(SESSION) == "running"
    assert sandbox.calls == ["create", "pause", "resume"]
    assert sinks.lifecycle[-1].phase == "resumed"


def test_ensure_recreates_from_terminal_phases(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """S2：不在册（killed / crashed 终态）且无持久 id 时 ensure 走 create 拉新实例。"""
    first = as_fake(manager.ensure(SESSION, provider="bailian")[0])
    manager.release(SESSION)
    assert manager.stable_phase(SESSION) == "killed"

    fresh, needs_reload = manager.ensure(SESSION, provider="bailian")

    assert needs_reload is False  # 无既有现场可丢：普通拉新不告知重载
    assert fresh is not first
    assert len(created_sandboxes) == 2
    assert manager.stable_phase(SESSION) == "running"

    # crashed 同样可拉新（AC-002-4 之后新 run 的恢复路径）
    manager.mark_crashed(SESSION, reason="剧本设定：不可用")
    third, _needs = manager.ensure(SESSION, provider="bailian")

    assert third is not fresh
    assert len(created_sandboxes) == 3
    assert manager.stable_phase(SESSION) == "running"


# --------------------------------------------------------------------- S3 adopt 重连


def test_ensure_adopts_persisted_sandbox(tmp_root: Path, sinks: RecordingSinks) -> None:
    """S3：不在册 + 持久 id 非空 → 工厂新建适配器后 adopt 重连（不 create、不 provision）。

    FakeSandbox.adopt 会把实例 id 换成被 adopt 的持久 id——断言 id 即证明重连到了
    「上一进程生命周期」的沙箱；相位 running + running 计费段 + reason=adopt 的事件。
    """
    created: list[FakeSandbox] = []

    def adopt_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            sandbox_id=f"sbx-unrelated-{len(created) + 1}",  # adopt 成功前 id 与持久值无关
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=adopt_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )

    client, needs_reload = manager.ensure(
        SESSION, provider="bailian", persisted_sandbox_id="sbx-persisted-9"
    )

    assert needs_reload is False
    assert client is created[0]
    assert client.id == "sbx-persisted-9"  # adopt 把实例重连到持久 id
    assert client.sandbox_id == "sbx-persisted-9"
    assert as_fake(client).calls == ["adopt"]  # 没有 create：provider 侧不新建
    assert len(created) == 1
    assert manager.phase(SESSION) == "running"
    assert manager.stable_phase(SESSION) == "running"
    assert manager.provider(SESSION) == "bailian"
    assert sinks.phases == ["running"]  # 无 creating 迁移（沙箱早已存在）
    assert sinks.lifecycle[0].reason == "adopt"
    assert sinks.lifecycle[0].sandbox_id == "sbx-persisted-9"
    open_segment = manager.open_usage_segment(SESSION)
    assert open_segment is not None and open_segment.phase == "running"


def test_ensure_adopt_reuses_existing_ledger_state(tmp_root: Path, sinks: RecordingSinks) -> None:
    """S3 账本原则：既有 `_SessionState` 复用不重建——计费分段历史跨 adopt 保留。

    场景：adopt 成功 → release（封口一段、phase=killed）→ 再次 ensure 同一持久 id
    （模拟又一轮重启）。若 adopt 分支无脑新建账本，第一段计费记录会丢。
    """
    created: list[FakeSandbox] = []

    def adopt_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root, provider=provider, session_id=session_id, sandbox_id="sbx-persisted-9"
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=adopt_factory, idle_pause_seconds=60, on_usage_segment=sinks.on_usage
    )
    manager.ensure(SESSION, provider="bailian", persisted_sandbox_id="sbx-persisted-9")
    manager.release(SESSION)
    assert len(manager.usage_segments(SESSION)) == 1  # release 封口了一段 running

    client, needs_reload = manager.ensure(
        SESSION, provider="bailian", persisted_sandbox_id="sbx-persisted-9"
    )

    assert needs_reload is False
    assert client is created[1]  # 第二只适配器实例（killed 后不在册，重新 adopt）
    segments = manager.usage_segments(SESSION)
    assert len(segments) == 1  # release 封口的历史段保留（账本未被 adopt 分支重建清空）
    assert segments[0].segment.duration_seconds is not None
    assert manager.open_usage_segment(SESSION) is not None  # 本轮 adopt 已开新的 running 段
    assert manager.provider(SESSION) == "bailian"  # 固化值未被调用方传参冲掉


def test_ensure_adopt_failure_falls_back_to_rebuild(tmp_root: Path, sinks: RecordingSinks) -> None:
    """S3：adopt 失败（持久 id 已被回收）→ 走既有 rebuild：recreated 事件 + needs_reload。"""
    created: list[FakeSandbox] = []

    def adopt_failing_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            sandbox_id=f"sbx-fresh-{len(created) + 1}",
            fail_adopt=len(created) == 0,  # 只有 adopt 尝试（第一只）失败
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=adopt_failing_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )

    client, needs_reload = manager.ensure(
        SESSION, provider="bailian", persisted_sandbox_id="sbx-dead-id"
    )

    assert needs_reload is True  # 无 restore 回调：明确告知需重载现场
    assert client is created[1]  # adopt 失败的实例被丢弃，rebuild 换了新实例
    assert as_fake(client).calls == ["create"]  # 重建走 create，不再 adopt
    assert created[0].calls == ["adopt"]  # 第一次尝试确实发起了 adopt
    assert len(created) == 2
    assert manager.phase(SESSION) == "recreated"
    assert manager.stable_phase(SESSION) == "running"
    assert manager.get(SESSION) is client  # 重建实例已在册（后续 ensure 复用它）
    assert [event.phase for event in sinks.lifecycle] == ["recreated"]
    assert "adopt 失败" in (sinks.lifecycle[0].reason or "")
    assert sinks.lifecycle[0].sandbox_id == client.id  # 新 id 随事件外发（桥接写回 DB 面）


def test_ensure_prefers_registered_instance_over_persisted_id(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """S3 决策矩阵：在册实例优先——持久 id 只在「不在册」时才参与 adopt 分支。"""
    first = as_fake(manager.create(SESSION, provider="bailian"))

    client, needs_reload = manager.ensure(
        SESSION, provider="bailian", persisted_sandbox_id="sbx-elsewhere"
    )

    assert needs_reload is False
    assert client is first
    assert first.calls == ["create"]  # 未发起 adopt（在册即复用）
    assert len(created_sandboxes) == 1


# =============================================================== 懒加载（bug 修复）
#
# run 车道改走 `ensure_lazy`：登记未 provision 实例（零网络），真实 provision 推迟到
# 首次实际使用（适配器 `_ensure_provisioned`）并经 provision 通知回调落相位/计费段/
# 事件。核心不变量：
# ① 纯聊天 run 全程零沙箱调用；② 首次工具调用才 create/adopt 且恰好一次；
# ③ paused 会话纯聊天不 resume、首工具调用 resume 一次；④ provision 失败 = 工具错误
# + crashed + 无悬挂计费段；⑤ 未 provision 实例的 release/shutdown/sweeper/status/probe
# 零网络不炸；⑥ adopt 懒触发（持久 id 在首工具调用时消费）。


def test_ensure_lazy_registers_unprovisioned_without_network(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """懒加载①：ensure_lazy 只登记——零网络调用、相位 none、零事件、零计费段。"""
    sandbox = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))

    assert manager.get(SESSION) is sandbox
    assert manager.sessions() == (SESSION,)
    assert manager.provider(SESSION) == "bailian"
    assert manager.phase(SESSION) == "none"
    assert manager.stable_phase(SESSION) == "none"
    assert sandbox.calls == []  # 零网络：无 create/adopt/status
    assert sandbox.state.created is False
    assert sandbox.provisioned is False
    assert sandbox.id == f"bailian:pending:{SESSION}"  # 占位 id（未 provision 口径不变）
    assert sinks.phases == []  # 零生命周期事件
    assert manager.open_usage_segment(SESSION) is None  # 零计费段
    assert manager.usage_segments(SESSION) == ()


def test_ensure_lazy_reuses_registered_instance(
    manager: SandboxManager, created_sandboxes: list[FakeSandbox]
) -> None:
    """懒加载：重复 ensure_lazy 复用同一实例（工厂只构造一次、零网络）。"""
    first = manager.ensure_lazy(SESSION, provider="bailian")
    second = manager.ensure_lazy(SESSION, provider="bailian")

    assert first is second is created_sandboxes[0]
    assert len(created_sandboxes) == 1
    assert as_fake(first).calls == []


def test_ensure_lazy_first_command_provisions_exactly_once(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """懒加载②：首次实际使用（命令）才 create 且恰好一次；相位/事件/计费段同步迁移。"""
    sandbox = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))
    assert sinks.phases == []

    result = manager.run_command(SESSION, CDM_ECHO_HELLO, exec_id=TOOL_CALL_ID)

    assert result.exit_code == 0
    assert "hello" in result.output
    assert sandbox.calls == ["create"]  # 首次使用才 create，恰好一次
    assert manager.phase(SESSION) == "running"
    assert manager.stable_phase(SESSION) == "running"
    assert sinks.phases == ["creating", "running"]  # 懒触发路径同样一对相位事件
    assert sinks.lifecycle[0].sandbox_id is None
    assert sinks.lifecycle[1].sandbox_id == sandbox.id
    open_segment = manager.open_usage_segment(SESSION)
    assert open_segment is not None and open_segment.phase == "running"

    manager.run_command(SESSION, CDM_ECHO_TICK)  # 幂等：不重复 provision
    assert sandbox.calls == ["create"]
    assert sinks.phases == ["creating", "running"]


def test_ensure_lazy_paused_stays_paused_until_first_use(
    manager: SandboxManager, sinks: RecordingSinks, fake_clock: FakeClock
) -> None:
    """懒加载③：paused 会话的 run 保持 paused（不网络 resume）；首工具调用 resume 一次。"""
    sandbox = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))
    manager.run_command(SESSION, CDM_ECHO_HELLO)  # 首 run 实际使用 → create
    fake_clock.advance(60)
    assert manager.evaluate_idle() == (SESSION,)
    assert manager.stable_phase(SESSION) == "paused"

    again = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))  # 下一 run：纯聊天起点

    assert again is sandbox
    assert manager.stable_phase(SESSION) == "paused"  # 保持 paused，不做网络 resume
    assert sandbox.calls == ["create", "pause"]

    manager.run_command(SESSION, CDM_ECHO_TICK, exec_id=TOOL_CALL_ID_B)  # 首工具调用：懒 resume

    assert manager.stable_phase(SESSION) == "running"
    assert sandbox.calls == ["create", "pause", "resume"]
    assert sinks.lifecycle[-1].phase == "resumed"
    # 计费分段：running → paused 已封口，resume 另开 running 段
    segments = [item.segment for item in manager.usage_segments(SESSION)]
    assert [segment.phase for segment in segments] == ["running", "paused"]
    open_segment = manager.open_usage_segment(SESSION)
    assert open_segment is not None and open_segment.phase == "running"


def test_ensure_lazy_adopts_persisted_id_on_first_use(
    tmp_root: Path, sinks: RecordingSinks
) -> None:
    """懒加载⑥：持久 id 存在 → 首工具调用走 adopt 而非 create；事件 reason=adopt。"""
    created: list[FakeSandbox] = []

    def adopt_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root, provider=provider, session_id=session_id, sandbox_id="sbx-unrelated"
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=adopt_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )

    client = manager.ensure_lazy(
        SESSION, provider="bailian", persisted_sandbox_id="sbx-persisted-9"
    )

    assert created[0].calls == []  # 登记阶段零网络（不 adopt）
    assert manager.phase(SESSION) == "none"

    manager.run_command(SESSION, CDM_ECHO_HELLO)

    assert created[0].calls == ["adopt"]  # 首工具调用 adopt，恰好一次、无 create
    assert client.id == "sbx-persisted-9"
    assert client.sandbox_id == "sbx-persisted-9"
    assert manager.phase(SESSION) == "running"
    assert sinks.phases == ["creating", "running"]
    assert sinks.lifecycle[1].reason == "adopt"
    assert sinks.lifecycle[1].sandbox_id == "sbx-persisted-9"
    assert manager.open_usage_segment(SESSION) is not None


def test_ensure_lazy_adopt_failure_falls_back_to_create(
    tmp_root: Path, sinks: RecordingSinks
) -> None:
    """P1 自愈：持久 id 失效 → 首次使用 adopt 失败 → 同一次调用内降级 create 兜底。

    成功通知 reason="recreated"（对齐 eager adopt→rebuild 的 AC-002-4 告知语义）；
    相位 running、running 计费段照常开。
    """
    created: list[FakeSandbox] = []

    def fallback_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            sandbox_id="sbx-fresh-1",
            fail_adopt=True,
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=fallback_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    client = manager.ensure_lazy(SESSION, provider="bailian", persisted_sandbox_id="sbx-dead-id")

    result = manager.run_command(SESSION, CDM_ECHO_HELLO)

    assert result.exit_code == 0
    assert created[0].calls == ["adopt", "create"]  # adopt 一次 + 降级 create 一次
    assert client.id == "sbx-fresh-1"
    assert manager.phase(SESSION) == "running"
    assert sinks.phases == ["creating", "running"]
    assert sinks.lifecycle[1].reason == "recreated"
    assert sinks.lifecycle[1].sandbox_id == "sbx-fresh-1"
    assert manager.open_usage_segment(SESSION) is not None


def test_ensure_lazy_adopt_fallback_create_failure_latches_crashed(
    sinks: RecordingSinks, tmp_root: Path
) -> None:
    """P1 烙死交互：adopt 失败 → 降级 create 也失败 → 最终失败才烙死 + crashed。"""
    created: list[FakeSandbox] = []

    def failing_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            fail_adopt=True,
            fail_create=True,
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=failing_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    manager.ensure_lazy(SESSION, provider="bailian", persisted_sandbox_id="sbx-dead-id")

    with pytest.raises(SandboxUnavailable):
        manager.run_command(SESSION, CDM_ECHO_HELLO)

    assert created[0].calls == ["adopt", "create"]  # 降级 create 恰好尝试一次
    assert manager.phase(SESSION) == "crashed"
    assert manager.get(SESSION) is None
    assert manager.open_usage_segment(SESSION) is None
    assert manager.usage_segments(SESSION) == ()
    assert sinks.phases == ["creating", "crashed"]


def test_late_running_notice_after_release_kills_orphan(
    manager: SandboxManager, sinks: RecordingSinks, created_sandboxes: list[FakeSandbox]
) -> None:
    """P2-1：release 与在途 provision 的窄竞态——迟到 running 通知触发 best-effort kill。

    全程公开 API 模拟：release 零网络摘除后，工具线程的懒 provision 恰好完成——create
    成功、通知迟到；manager 按身份判伪丢弃通知，并把泄漏实例第一时间 kill。
    """
    sandbox = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))
    manager.release(SESSION, reason="user")  # 未 provision → 零网络摘除（窄竞态前提）
    assert manager.phase(SESSION) == "killed"

    result = sandbox.run_command(CDM_ECHO_HELLO)  # 懒 provision 在摘除之后才完成

    assert result.exit_code == 0
    assert sandbox.calls == ["create", "kill"]  # 泄漏实例第一时间被 best-effort kill
    assert manager.get(SESSION) is None
    assert manager.phase(SESSION) == "killed"  # 迟到通知不改写已终态账本


def test_ensure_lazy_reregistration_clears_stale_sandbox_id(
    manager: SandboxManager, sinks: RecordingSinks
) -> None:
    """P2-2：crashed 复用账本重登记后，creating 生命周期事件不得携带陈旧 sandbox_id。"""
    manager.ensure_lazy(SESSION, provider="bailian")
    manager.run_command(SESSION, CDM_ECHO_HELLO)  # provision → 账本记下实例 id
    manager.mark_crashed(SESSION, reason="剧本设定")  # 账本残留旧 sandbox_id

    manager.ensure_lazy(SESSION, provider="bailian")  # 重登记新实例
    manager.run_command(SESSION, CDM_ECHO_TICK)  # 懒 provision → 第二次 creating 事件

    creating = [event for event in sinks.lifecycle if event.phase == "creating"]
    assert len(creating) == 2
    assert creating[-1].sandbox_id is None  # 不得携带上一实例的陈旧 id 上 wire


def test_ensure_lazy_provision_failure_marks_crashed_without_segment(
    sinks: RecordingSinks, tmp_root: Path
) -> None:
    """懒加载④：provision 失败 → 工具错误 + crashed + 无悬挂计费段；可重新懒登记。"""
    created: list[FakeSandbox] = []

    def failing_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = FakeSandbox(
            root=tmp_root, provider=provider, session_id=session_id, fail_create=True
        )
        created.append(sandbox)
        return sandbox

    manager = SandboxManager(
        factory=failing_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    manager.ensure_lazy(SESSION, provider="bailian")

    with pytest.raises(SandboxUnavailable):
        manager.run_command(SESSION, CDM_ECHO_HELLO)

    assert created[0].calls == ["create"]  # 恰好一次 provision 尝试
    assert manager.phase(SESSION) == "crashed"
    assert manager.stable_phase(SESSION) == "crashed"
    assert manager.get(SESSION) is None
    assert manager.open_usage_segment(SESSION) is None  # 无悬挂计费段
    assert manager.usage_segments(SESSION) == ()
    assert sinks.phases == ["creating", "crashed"]

    # crashed 后重新懒登记：拉新实例（账本复用、相位回 none），不是 rebuild 旧实例
    fresh = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))
    assert fresh is created[1]
    assert manager.phase(SESSION) == "none"


def test_ensure_lazy_release_shutdown_idle_are_zero_network(
    manager: SandboxManager,
    sinks: RecordingSinks,
    created_sandboxes: list[FakeSandbox],
    fake_clock: FakeClock,
) -> None:
    """懒加载⑤：release/shutdown/sweeper 对未 provision 实例零网络、零误伤。"""
    sandbox = as_fake(manager.ensure_lazy(SESSION, provider="bailian"))

    manager.shutdown()
    fake_clock.advance(600)
    assert manager.evaluate_idle() == ()
    assert sandbox.calls == []  # shutdown / 空闲清扫均跳过未 provision 实例
    assert manager.phase(SESSION) == "none"

    manager.release(SESSION, reason="user")

    assert sandbox.calls == []  # release 零网络（provider 侧无实例可 kill）
    assert manager.phase(SESSION) == "killed"
    assert manager.get(SESSION) is None
    assert manager.sessions() == ()
    assert manager.open_usage_segment(SESSION) is None  # 无计费段可封
    assert sinks.lifecycle[-1].phase == "killed"


def test_status_and_probe_of_unprovisioned_instance(manager: SandboxManager) -> None:
    """懒加载⑤：未 provision 实例的 status/probe 如实回报 none，不炸也不误标 crashed。"""
    as_fake(manager.ensure_lazy(SESSION, provider="bailian"))

    status = manager.status(SESSION)
    assert status.phase == "none"
    assert status.sandbox_id is None
    assert status.available is False

    probe = manager.probe(SESSION)
    assert probe.phase == "none"
    assert probe.available is False
    assert manager.phase(SESSION) == "none"  # 未 provision ≠ 不可用，不得标 crashed


def test_ensure_different_sessions_do_not_block(tmp_root: Path, sinks: RecordingSinks) -> None:
    """S2 分片锁：会话 A 的 provision 卡在 gate 时，会话 B 的 ensure 必须照常完成。

    若锁退化成全局一把（粒度错误），B 会等 A 的 gate 超时（10s）——用 B 的耗时上界
    断言区分「分片锁」与「全局锁」两种实现。
    """
    gate_a = threading.Event()
    sandboxes: dict[str, _SlowCreateSandbox] = {}

    def gated_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
        sandbox = _SlowCreateSandbox(
            root=tmp_root,
            provider=provider,
            session_id=session_id,
            create_gate=gate_a if session_id == SESSION else None,
        )
        sandboxes[session_id] = sandbox
        return sandbox

    manager = SandboxManager(
        factory=gated_factory, idle_pause_seconds=60, on_lifecycle=sinks.on_lifecycle
    )
    results: list[LcodeSandbox] = []

    def worker_a() -> None:
        client, _needs = manager.ensure(SESSION, provider="bailian")
        results.append(client)

    thread_a = threading.Thread(target=worker_a, name="ensure-A")
    thread_a.start()
    sandbox_a: _SlowCreateSandbox | None = None
    for _ in range(200):  # 等 A 真正进入 create（在 gate 上等待）
        candidate = sandboxes.get(SESSION)
        if candidate is not None and candidate.enter_create.wait(timeout=0.05):
            sandbox_a = candidate
            break
        sleep(0.01)  # 字典项尚未出现时让出 GIL，防止空转烧完轮数
    assert sandbox_a is not None, "A 未在期限内进入 provision"

    start = perf_counter()
    ensured_b, _needs_b = manager.ensure(SESSION_B, provider="bailian")
    elapsed_b = perf_counter() - start

    assert elapsed_b < 2.0, f"ensure(B) 被 A 的 provision 阻塞 {elapsed_b:.2f}s（分片失效）"
    assert manager.get(SESSION_B) is ensured_b
    assert sandboxes[SESSION_B].state.created is True
    assert manager.get(SESSION) is None  # A 仍在 gate 上：未登记，B 也没等它

    gate_a.set()
    thread_a.join(timeout=5)
    assert not thread_a.is_alive(), "A 的 ensure 未在放行后收敛"
    assert results[0] is sandboxes[SESSION]
    assert manager.get(SESSION) is sandboxes[SESSION]
    assert sandboxes[SESSION].calls == ["create"]  # A 的 provision 只发生一次


class _RecordingEnsureManager:
    """记录 ensure_lazy 调用线程与入参的 manager 替身（run 层 to_thread 语义断言用）。"""

    def __init__(self) -> None:
        self.backend = object()
        self.calls: list[tuple[int, str, str | None, str | None]] = []

    def ensure_lazy(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        persisted_sandbox_id: str | None = None,
    ) -> object:
        self.calls.append((threading.get_ident(), session_id, provider, persisted_sandbox_id))
        return self.backend


async def test_run_service_ensure_backend_offloads_to_thread() -> None:
    """S1：_ensure_backend 经 asyncio.to_thread 卸载——ensure_lazy 不在事件循环线程执行。

    同时锁住入参转发：launch 的 session_id / provider / persisted_sandbox_id 原样到达
    manager.ensure_lazy（懒加载语义：只登记不 provision，persisted id 作 adopt 注入）。
    """
    loop_thread = threading.get_ident()
    fake_manager = _RecordingEnsureManager()
    service = RunService(
        cast("Any", None), pipeline=cast("Any", None), manager=cast("Any", fake_manager)
    )
    launch = RunLaunch(
        session_id=SESSION,
        run_id="33333333-3333-4333-8333-333333333333",
        content="你好",
        model="qwen3.7-plus",
        reasoning_effort="off",
        provider="bailian",
        persisted_sandbox_id="sbx-from-previous-life",
    )

    backend = await service._ensure_backend(launch)

    assert backend is fake_manager.backend
    assert len(fake_manager.calls) == 1
    call_thread, call_session, call_provider, call_persisted = fake_manager.calls[0]
    assert call_thread != loop_thread, "ensure_lazy 在事件循环线程上执行了（to_thread 未生效）"
    assert call_session == SESSION
    assert call_provider == "bailian"
    assert call_persisted == "sbx-from-previous-life"


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
    connect_factory: Callable[..., Any] | None = None,
    image: str = "python:3.11",
    **cfg_overrides: str | int | float,
) -> OpenSandboxSandbox:
    return OpenSandboxSandbox(
        settings=_cfg(**cfg_overrides),
        image=image,
        sandbox_factory=lambda *args, **kwargs: handle,
        resume_factory=resume_factory,
        connect_factory=connect_factory,
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


# ------------------------------------------------------------ 适配器懒 provision


def test_bailian_lazy_provision_creates_on_first_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """懒加载②（适配器面）：未 create 即执行命令 → 先 create 再跑命令，恰好一次。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle(sandbox_id="e2b-lazy-1", stdout_chunks=("lazy-out\n",))
    factory_calls: list[int] = []

    def factory(**kwargs: object) -> _FakeE2BHandle:
        factory_calls.append(1)
        return handle

    notices: list[str] = []
    sandbox = BailianSandbox(settings=_cfg(), sandbox_factory=factory)
    sandbox.set_provision_observer(lambda notice: notices.append(notice.phase))

    result = sandbox.run_command("ls")

    assert result.output == "lazy-out\n"
    assert len(factory_calls) == 1  # create 恰好一次
    assert sandbox.provisioned is True
    assert sandbox.id == "e2b-lazy-1"
    assert notices == ["creating", "running"]

    sandbox.run_command("ls")  # 幂等：不再触发任何 provision 通知
    assert len(factory_calls) == 1
    assert notices == ["creating", "running"]


def test_bailian_lazy_provision_resumes_paused_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """懒加载③（适配器面）：paused 实例首次使用时懒 resume（handle.connect），不 create。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeE2BHandle()
    sandbox = _bailian(handle)
    sandbox.create()
    sandbox.pause()
    assert handle.calls == ["pause"]

    notices: list[str] = []
    sandbox.set_provision_observer(lambda notice: notices.append(notice.phase))

    result = sandbox.run_command("ls")

    assert result.exit_code == 0
    assert handle.calls == ["pause", "connect", "run"]  # resume 走 connect，无第二次 create
    assert notices == ["resumed"]
    assert sandbox.paused is False


def test_bailian_lazy_provision_failure_latches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """懒加载④（适配器面）：provision 失败烙死——工厂不再被二次调用（防孤儿沙箱）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    factory_calls: list[int] = []
    auth = AuthenticationException("401")

    def factory(**kwargs: object) -> _FakeE2BHandle:
        factory_calls.append(1)
        raise auth

    notices: list[str] = []
    sandbox = BailianSandbox(settings=_cfg(), sandbox_factory=factory)
    sandbox.set_provision_observer(lambda notice: notices.append(notice.phase))

    with pytest.raises(SandboxConfigurationError):
        sandbox.run_command("ls")
    with pytest.raises(SandboxConfigurationError):
        sandbox.run_command("ls")

    assert len(factory_calls) == 1
    assert notices == ["creating", "crashed"]


def test_bailian_lazy_adopt_failure_falls_back_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1（适配器面）：adopt 报 SandboxNotFound → 同一次首次使用内降级 create 兜底。

    成功通知 (running, reason="recreated")；persist id 只消费一次，不再重试 adopt。
    """
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    fresh = _FakeE2BHandle(sandbox_id="e2b-fresh-1")
    calls: list[str] = []

    def connect_factory(sandbox_id: str, **kwargs: object) -> _FakeE2BHandle:
        calls.append(f"adopt:{sandbox_id}")
        raise SandboxNotFoundException(f"404: {sandbox_id}")

    def sandbox_factory(**kwargs: object) -> _FakeE2BHandle:
        calls.append("create")
        return fresh

    notices: list[tuple[str, str | None]] = []
    sandbox = BailianSandbox(
        settings=_cfg(), connect_factory=connect_factory, sandbox_factory=sandbox_factory
    )
    sandbox.set_pending_adopt_id("e2b-dead-1")
    sandbox.set_provision_observer(lambda notice: notices.append((notice.phase, notice.reason)))

    result = sandbox.run_command("ls")

    assert result.exit_code == 0
    assert calls == ["adopt:e2b-dead-1", "create"]  # 降级 create 恰好一次
    assert sandbox.id == "e2b-fresh-1"
    assert notices == [("creating", None), ("running", "recreated")]


def test_opensandbox_lazy_provision_creates_on_first_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """懒加载②（opensandbox 面）：未 create 即执行命令 → 先 create 再跑命令，恰好一次。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    handle = _FakeOSHandle(execution_text="os-lazy\n")
    factory_calls: list[int] = []

    def factory(*args: Any, **kwargs: Any) -> _FakeOSHandle:
        factory_calls.append(1)
        return handle

    notices: list[str] = []
    sandbox = OpenSandboxSandbox(settings=_cfg(), image="python:3.11", sandbox_factory=factory)
    sandbox.set_provision_observer(lambda notice: notices.append(notice.phase))

    result = sandbox.run_command("ls")

    assert result.output == "os-lazy\n"
    assert len(factory_calls) == 1
    assert sandbox.provisioned is True
    assert notices == ["creating", "running"]


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


# -------------------------------------------------------------------- S3 适配器 adopt


def test_bailian_adopt_connects_by_id_with_connection_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3：adopt 走注入的 connect 替身，kwargs = 连接参数面（无 template/timeout）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    captured: dict[str, object] = {}

    def connect_factory(sandbox_id: str, **kwargs: object) -> _FakeE2BHandle:
        captured["sandbox_id"] = sandbox_id
        captured.update(kwargs)
        return _FakeE2BHandle(sandbox_id=sandbox_id)

    sandbox = BailianSandbox(
        settings=_cfg(bailian_template="tmpl-x"),  # template 只属于 create，adopt 不带
        connect_factory=connect_factory,
    )

    sandbox.adopt("e2b-adopt-1")

    assert captured["sandbox_id"] == "e2b-adopt-1"
    assert captured["api_url"] == FAKE_BAILIAN_API_URL
    # 占位 api_key + 真实鉴权头 + 关闭客户端预校验（与 create 同源，NFR-004）
    assert captured["api_key"] == "e2b_0"
    assert captured["headers"] == {"Authorization": "Bearer unit-test-fake-key"}
    assert captured["validate_api_key"] is False
    assert "template" not in captured
    assert "timeout" not in captured
    assert sandbox.id == "e2b-adopt-1"
    assert sandbox.sandbox_id == "e2b-adopt-1"


def test_bailian_adopt_missing_sandbox_translates_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3：持久 id 已被回收 → SandboxNotFoundException 归一为 SandboxUnavailable（manager 转重建的输入）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")

    def connect_factory(sandbox_id: str, **kwargs: object) -> _FakeE2BHandle:
        raise SandboxNotFoundException(f"404: sandbox {sandbox_id} not found")

    sandbox = BailianSandbox(settings=_cfg(), connect_factory=connect_factory)

    with pytest.raises(SandboxUnavailable):
        sandbox.adopt("e2b-gone")


def test_bailian_adopt_rejects_provisioned_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3：已 provision 的实例 adopt 与 create 互斥（相位机编程错误面）。"""
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    sandbox = _bailian(_FakeE2BHandle())
    sandbox.create()

    with pytest.raises(SandboxLifecycleError):
        sandbox.adopt("e2b-adopt-2")


def test_opensandbox_adopt_connects_by_id_via_connect_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3：adopt 走 **connect** 类方法注入点按 id 取句柄（无状态迁移；running 沙箱直连）。

    不走 resume：resume 是服务端 paused 语义的状态迁移（评审项 1），对 running 沙箱
    会拒绝/未定义——替身按 connect 语义只回句柄、不模拟状态迁移。
    """
    monkeypatch.setenv(FAKE_KEY_ENV, "unit-test-fake-key")
    new = _FakeOSHandle(sandbox_id="os-adopt-1", state="RUNNING")
    captured: dict[str, object] = {}

    def connect_factory(sandbox_id: str, **kwargs: object) -> _FakeOSHandle:
        captured.update({"sandbox_id": sandbox_id, **kwargs})
        return new

    sandbox = _opensandbox(_FakeOSHandle(sandbox_id="os-old"), connect_factory=connect_factory)

    sandbox.adopt("os-adopt-1")

    assert captured["sandbox_id"] == "os-adopt-1"
    assert captured["connection_config"] is not None
    assert sandbox.id == "os-adopt-1"
    assert sandbox.status().phase == "running"  # 状态读到的是新句柄（行为面证明句柄已换）
    # 已 provision 的实例 adopt 与 create 互斥
    with pytest.raises(SandboxLifecycleError):
        sandbox.adopt("os-adopt-2")
