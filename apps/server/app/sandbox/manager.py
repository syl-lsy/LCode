"""SandboxManager：会话级沙箱生命周期编排（A3 §4.3；REQ-002 / AC-002-1~6、AC-015-4）。

职责（与 base.py 的分工）：manager 管**相位与成本**，adapter 管**协议与原语**——

- **注册表**：`session_id → LcodeSandbox`（`_live`），另存一份会话账本（`_states`：
  provider / 相位 / 计费分段 / 活跃 run 数），账本在沙箱释放后仍可查（usage 对账、
  重建需要 provider 固化值）；
- **相位机**：`none → creating → running ⇄ paused/resumed → killed/crashed/recreated`
  （事件侧 7 值域 + none；查询侧 `stable_phase()` 把 resumed/recreated 折叠成 running，
  对齐契约 `SandboxStatus.phase` 的既定口径）。每次迁移发 `SandboxLifecycleEvent`
  （上游 service 转成 `sandbox_output(channel=lifecycle)` 域事件——本层不 import events/）；
- **空闲 pause**（AC-002-2）：`evaluate_idle()` 由 lifespan 周期调用；`idle_pause_seconds`
  到阈值且 **run 活跃计数为 0**（Q3：run running/streaming 期间永不 pause）才显式
  `pause()`——百炼口径：只有显式调用才保留状态（硬约束 8）；
- **意外不可用**（AC-002-4）：`run_command` 捕获 `SandboxUnavailable` / `probe()` 探测
  provider 不可用 → 相位落 `crashed` 并 `rebuild()`（重建可带 `restore` 回调重放载入
  现场；无回调则如实标 `needs_reload`，"明确告知需重克隆"，不静默丢弃）；
- **provider 会话级固化**（AC-002-6）：`create(provider=None)` 时读当前全局配置并记入
  账本；此后该会话的重建沿用账本值，改全局配置只影响新会话；
- **计费分段**（AC-015-4）：每次进入 running/paused 开段、离开时封口（时长取
  `clock.utc_now()` 差值，**金额估算归 cost_service**）；封口段经 `on_usage_segment`
  推给上层落 `sandbox_usage` 表，并留存在账本供查询；
- **关停策略**（U9）：`shutdown()` 对 running 沙箱显式 `pause()`（保留状态，重启可续）；
  会话 DELETE → `release()` 即 `kill()`（AC-002-3，幂等）。

时钟：全部取 `app.utils.clock.utc_now()`（模块属性调用时解析）——unit 层 patch 它即可
推进假时钟（test-plan §3.6）。本模块为同步实现：适配器与计费段都是廉价操作，真正的
异步边界在 run 执行层（另一车道）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, cast

from deepagents.backends.protocol import ExecuteResponse

from app.core.config import sandbox_settings
from app.core.logger import BusinessLog
from app.sandbox.base import (
    LcodeSandbox,
    SandboxError,
    SandboxLifecycleError,
    SandboxOutputChunk,
    SandboxPhase,
    SandboxProvider,
    SandboxStatus,
    SandboxUnavailable,
    SandboxUsageSegment,
    StableSandboxPhase,
)
from app.sandbox.factory import create_sandbox_client
from app.utils import clock

# --------------------------------------------------------------------------- 注入面类型

SandboxFactory = Callable[[SandboxProvider, str], LcodeSandbox]
"""工厂签名：`(provider, session_id) -> 未 provision 的沙箱客户端`。"""

LifecycleSink = Callable[["SandboxLifecycleEvent"], None]
OutputSink = Callable[[str, SandboxOutputChunk], None]
UsageSink = Callable[["SandboxUsageRecord"], None]
RestoreCallback = Callable[[LcodeSandbox], None]

_RUNNING_LIKE: frozenset[str] = frozenset({"running", "resumed", "recreated"})
"""功能上的运行态（可执行命令、可被空闲 pause）；resumed/recreated 是迁移动作。"""

_TERMINAL: frozenset[str] = frozenset({"none", "killed", "crashed"})
"""允许重新 create 的账本相位（终态/无实例）；其余相位重复 create 视为编程错误。"""


# --------------------------------------------------------------------------- 事件/记录


@dataclass(frozen=True, slots=True)
class SandboxLifecycleEvent:
    """一次相位迁移（上游 service 转成 `sandbox_output(channel=lifecycle)` 域事件）。"""

    session_id: str
    phase: SandboxPhase
    provider: SandboxProvider
    sandbox_id: str | None = None
    reason: str | None = None
    at: datetime = field(default_factory=clock.utc_now)


@dataclass(frozen=True, slots=True)
class SandboxUsageRecord:
    """一行 `sandbox_usage` 所需的全部字段（封口段；AC-015-4）。"""

    session_id: str
    provider: SandboxProvider
    sandbox_id: str
    segment: SandboxUsageSegment


@dataclass(frozen=True, slots=True)
class SandboxRebuildResult:
    """一次重建的产出（AC-002-4：恢复现场或明确告知需重载）。"""

    session_id: str
    provider: SandboxProvider
    previous_sandbox_id: str | None
    sandbox: LcodeSandbox
    restore_applied: bool
    needs_reload: bool


@dataclass
class _SessionState:
    """会话账本：沙箱实例之外必须**活过实例生命周期**的状态。"""

    session_id: str
    provider: SandboxProvider
    phase: SandboxPhase = "none"
    sandbox_id: str | None = None
    last_sandbox_id: str | None = None
    last_active_at: datetime = field(default_factory=clock.utc_now)
    active_runs: int = 0
    open_segment: SandboxUsageSegment | None = None
    segments: list[SandboxUsageRecord] = field(default_factory=list)


_STABLE_PHASE: dict[str, StableSandboxPhase] = {
    "none": "none",
    "creating": "creating",
    "running": "running",
    "paused": "paused",
    "resumed": "running",
    "killed": "killed",
    "crashed": "crashed",
    "recreated": "running",
}


def _stable(phase: SandboxPhase) -> StableSandboxPhase:
    """事件侧相位 → 查询侧稳态（契约：resumed/recreated 折叠成 running）。"""
    return _STABLE_PHASE[phase]


# --------------------------------------------------------------------------- manager


class SandboxManager:
    """`dict[session_id, LcodeSandbox]` + 相位机 + 空闲计时 + 计费分段 + 关停策略。"""

    def __init__(
        self,
        *,
        factory: SandboxFactory | None = None,
        idle_pause_seconds: float | None = None,
        on_lifecycle: LifecycleSink | None = None,
        on_output: OutputSink | None = None,
        on_usage_segment: UsageSink | None = None,
    ) -> None:
        self._factory: SandboxFactory = factory or _default_factory
        self._idle_pause_seconds = (
            idle_pause_seconds
            if idle_pause_seconds is not None
            else sandbox_settings.idle_pause_seconds
        )
        self._on_lifecycle = on_lifecycle
        self._on_output = on_output
        self._on_usage_segment = on_usage_segment
        self._live: dict[str, LcodeSandbox] = {}
        self._states: dict[str, _SessionState] = {}
        # F5：kill 失败后的重试意图（独立于 `_live`——死客户端不得经 get() 交给新 run）
        self._pending_kill: dict[str, LcodeSandbox] = {}

    # ---------------------------------------------------------------------- 注册表

    @property
    def idle_pause_seconds(self) -> float:
        """空闲 pause 阈值（秒）——lifespan 周期任务与测试断言共用。"""
        return self._idle_pause_seconds

    def sessions(self) -> tuple[str, ...]:
        """在册（存活）会话 id，按创建顺序。"""
        return tuple(self._live)

    def get(self, session_id: str) -> LcodeSandbox | None:
        """当前存活实例；crashed / killed / 未创建 = None（崩溃实例走 rebuild 换新）。"""
        return self._live.get(session_id)

    def require(self, session_id: str) -> LcodeSandbox:
        """取存活实例；没有即相位机错误（调用方必须先 create）。"""
        client = self._live.get(session_id)
        if client is None:
            raise SandboxLifecycleError(f"会话 {session_id} 没有在册沙箱（先 create）")
        return client

    def provider(self, session_id: str) -> SandboxProvider | None:
        """该会话固化的 provider（AC-002-6；未见过该会话 = None）。"""
        state = self._states.get(session_id)
        return state.provider if state else None

    # ---------------------------------------------------------------------- 相位机

    def phase(self, session_id: str) -> SandboxPhase:
        """事件侧相位（最后一次迁移；未见过该会话 = none）。"""
        state = self._states.get(session_id)
        return state.phase if state else "none"

    def stable_phase(self, session_id: str) -> StableSandboxPhase:
        """查询侧稳态（GET /sessions/{id}/sandbox 的 phase 口径）。"""
        return _stable(self.phase(session_id))

    def status(self, session_id: str) -> SandboxStatus:
        """provider 侧活性探测（不抛错；未注册会话返回 none 投影）。"""
        client = self._live.get(session_id)
        if client is None:
            return SandboxStatus(
                sandbox_id=None,
                phase="none",
                available=False,
                detail="未注册沙箱（尚无实例或已释放）",
            )
        return client.status()

    def probe(self, session_id: str) -> SandboxStatus:
        """探测并把"provider 报不可用"转成 crashed（AC-002-4 的被动检测路径）。"""
        client = self._live.get(session_id)
        if client is None:
            state = self._states.get(session_id)
            return SandboxStatus(
                sandbox_id=state.last_sandbox_id if state else None,
                phase=_stable(self.phase(session_id)),
                available=False,
                detail="无在册沙箱实例",
            )
        status = client.status()
        if not status.available:
            self.mark_crashed(session_id, reason=status.detail or "provider 报告不可用")
        return status

    # ---------------------------------------------------------------------- 创建

    def create(
        self,
        session_id: str,
        *,
        provider: SandboxProvider | None = None,
        timeout_seconds: int | None = None,
    ) -> LcodeSandbox:
        """为会话 provision 一个沙箱（provider 未传 = 用当前全局配置并固化到账本）。"""
        existing = self._states.get(session_id)
        if session_id in self._live or (existing is not None and existing.phase not in _TERMINAL):
            raise SandboxLifecycleError(
                f"会话 {session_id} 已有沙箱（phase={self.phase(session_id)}）；重建请走 rebuild()"
            )

        resolved = provider or cast("SandboxProvider", sandbox_settings.provider)
        client = self._factory(resolved, session_id)  # 未知 provider 在这里报配置错误
        state = _SessionState(session_id=session_id, provider=resolved, phase="creating")
        self._states[session_id] = state
        self._emit(state, "creating", reason=None)

        try:
            client.create(timeout_seconds=timeout_seconds)
        except SandboxError as exc:
            state.phase = "crashed"
            self._emit(state, "crashed", reason=exc.message)
            raise

        client.add_output_handler(lambda chunk: self._forward_output(session_id, chunk))
        self._live[session_id] = client
        state.phase = "running"
        state.sandbox_id = client.id
        state.last_sandbox_id = client.id
        state.last_active_at = clock.utc_now()
        self._open_segment(state, "running")
        self._emit(state, "running", reason=None)
        return client

    # ------------------------------------------------------------------ 命令与活动

    def run_command(
        self,
        session_id: str,
        command: str,
        *,
        exec_id: str | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """经 manager 执行一条命令：顺带刷新空闲计时，并把不可用转成 crashed + 重建入口。

        直接持有 backend（deepagents 工具路径）的调用方可不经本方法，但那时"意外不可用
        → crashed"需要显式调用 `note_unavailable()` 上报。
        """
        client = self.require(session_id)
        self.touch(session_id)
        try:
            return client.run_command(command, timeout=timeout, exec_id=exec_id)
        except SandboxUnavailable as exc:
            self.mark_crashed(session_id, reason=exc.message)
            raise

    def note_unavailable(self, session_id: str, *, reason: str) -> None:
        """旁路上报不可用（不经 manager 执行命令的路径用；AC-002-4）。"""
        self.mark_crashed(session_id, reason=reason)

    def touch(self, session_id: str, *, at: datetime | None = None) -> None:
        """刷新活跃时间（命令执行 / 输出块到达都算活动；Q3 口径）。"""
        self._require_state(session_id).last_active_at = at or clock.utc_now()

    def begin_run(self, session_id: str) -> None:
        """run 开始：活跃计数 +1（期间空闲 pause 被抑制）。"""
        state = self._require_state(session_id)
        state.active_runs += 1
        state.last_active_at = clock.utc_now()

    def end_run(self, session_id: str) -> None:
        """run 结束：活跃计数 -1 并重置空闲计时（刚结束不算空闲）。"""
        state = self._require_state(session_id)
        state.active_runs = max(state.active_runs - 1, 0)
        state.last_active_at = clock.utc_now()

    def active_runs(self, session_id: str) -> int:
        """该会话正在跑的 run 数（0 = 可被空闲 pause）。"""
        return self._require_state(session_id).active_runs

    # ------------------------------------------------------------------ 生命周期

    def pause(self, session_id: str, *, reason: str | None = None) -> None:
        """显式 pause（AC-002-2）：running/resumed/recreated → paused，计费段随之切换。"""
        state = self._require_state(session_id)
        if state.phase not in _RUNNING_LIKE:
            raise SandboxLifecycleError(
                f"会话 {session_id} 不能从 phase={state.phase} pause（只有运行态可以）"
            )
        client = self.require(session_id)
        try:
            client.pause()
        except SandboxUnavailable as exc:
            self.mark_crashed(session_id, reason=exc.message)
            raise
        self._close_segment(state)
        self._open_segment(state, "paused")
        state.phase = "paused"
        self._emit(state, "paused", reason=reason or "manual")

    def resume(self, session_id: str) -> LcodeSandbox:
        """从 paused 恢复（沿用同一实例，不重建；AC-002-2）。"""
        state = self._require_state(session_id)
        if state.phase != "paused":
            raise SandboxLifecycleError(
                f"会话 {session_id} 不能从 phase={state.phase} resume（只有 paused 可以）"
            )
        client = self.require(session_id)
        try:
            client.resume()
        except SandboxUnavailable as exc:
            self.mark_crashed(session_id, reason=exc.message)
            raise
        self._close_segment(state)
        self._open_segment(state, "running")
        state.phase = "resumed"
        state.last_active_at = clock.utc_now()
        self._emit(state, "resumed", reason="user")
        return client

    def release(self, session_id: str, *, reason: str = "user") -> None:
        """会话 DELETE → kill 沙箱并移出注册表（AC-002-3；未在册时幂等 no-op）。

        F5 释放顺序：**kill 成功后才摘注册表**；kill 失败时把实例放进独立的
        `_pending_kill` 重试意图（同时保证它不在 `_live`，`get()` 不会把死客户端交给
        新 run），并立即封口计费段——不悬挂计费。第二次 `release` 会取出 pending 再次
        kill，而不是因为 `_live` 已空就 no-op；`delete_session` 因异常冒泡而不 purge
        业务行，重试成功后才会清行。
        """
        state = self._states.get(session_id)
        if state is None:
            return
        pending = self._pending_kill.get(session_id)
        client = pending if pending is not None else self._live.get(session_id)
        if client is None:
            return
        try:
            client.kill()
        except SandboxUnavailable as exc:
            # 保留可重试的 kill 意图；若它恰是当前可服务实例，同时从 `_live` 摘除，
            # 避免 get() 把 crash 客户端交给新 run（identity 比较防止误摘后来新建的实例）
            if self._live.get(session_id) is client:
                del self._live[session_id]
            self._pending_kill[session_id] = client
            self._close_segment(state)
            state.phase = "crashed"
            self._emit(state, "crashed", reason=exc.message)
            raise
        self._pending_kill.pop(session_id, None)
        if self._live.get(session_id) is client:
            del self._live[session_id]
        self._close_segment(state)
        state.phase = "killed"
        self._emit(state, "killed", reason=reason)

    def mark_crashed(self, session_id: str, *, reason: str) -> None:
        """标记意外不可用（幂等）：移出注册表、封口计费段、run 计数清零（AC-002-4）。"""
        state = self._require_state(session_id)
        if state.phase in {"crashed", "killed"}:
            return
        client = self._live.pop(session_id, None)
        if client is not None:
            state.last_sandbox_id = client.id
        self._close_segment(state)
        state.active_runs = 0
        state.phase = "crashed"
        self._emit(state, "crashed", reason=reason)

    def rebuild(
        self,
        session_id: str,
        *,
        reason: str = "rebuild",
        restore: RestoreCallback | None = None,
        timeout_seconds: int | None = None,
    ) -> SandboxRebuildResult:
        """重建 crashed 沙箱（AC-002-4）：provider 沿用账本固化值，可带恢复回调。"""
        state = self._require_state(session_id)
        if state.phase != "crashed":
            raise SandboxLifecycleError(
                f"会话 {session_id} 不能从 phase={state.phase} rebuild（只有 crashed 可以）"
            )
        previous_sandbox_id = state.last_sandbox_id
        client = self._factory(state.provider, session_id)
        client.create(timeout_seconds=timeout_seconds)  # 失败：状态保持 crashed，向上抛
        restore_applied = False
        if restore is not None:
            restore(client)
            restore_applied = True

        client.add_output_handler(lambda chunk: self._forward_output(session_id, chunk))
        self._live[session_id] = client
        state.phase = "recreated"
        state.sandbox_id = client.id
        state.last_sandbox_id = client.id
        state.last_active_at = clock.utc_now()
        state.active_runs = 0
        self._open_segment(state, "running")
        self._emit(state, "recreated", reason=reason)
        return SandboxRebuildResult(
            session_id=session_id,
            provider=state.provider,
            previous_sandbox_id=previous_sandbox_id,
            sandbox=client,
            restore_applied=restore_applied,
            needs_reload=not restore_applied,
        )

    def shutdown(self) -> None:
        """服务进程关停：对运行态沙箱显式 pause（保状态可续，U9），不 kill。"""
        for session_id in list(self._live):
            state = self._states[session_id]
            if state.phase not in _RUNNING_LIKE:
                continue
            try:
                self.pause(session_id, reason="shutdown")
            except SandboxError as exc:  # 单个会话失败不阻断其余关停（已标 crashed）
                self._log(f"关停时 pause 失败（session={session_id}）", exc)

    def evaluate_idle(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """空闲检查（lifespan 周期调用）：到阈值且无活跃 run 才 pause，返回被暂停的会话。

        run 活跃期间**永不 pause**（Q3）；刚 end_run 的会话因计时被重置而不会立即暂停。
        """
        current = now or clock.utc_now()
        paused: list[str] = []
        for session_id, state in self._states.items():
            if state.phase not in _RUNNING_LIKE:
                continue
            if session_id not in self._live or state.active_runs > 0:
                continue
            idle_seconds = clock.elapsed_seconds(state.last_active_at, current)
            if idle_seconds >= self._idle_pause_seconds:
                self.pause(session_id, reason="idle")
                paused.append(session_id)
        return tuple(paused)

    # ------------------------------------------------------------------ 计费分段

    def usage_segments(self, session_id: str) -> tuple[SandboxUsageRecord, ...]:
        """该会话已封口的计费分段（AC-015-4；进行中的段用 `open_usage_segment`）。"""
        state = self._states.get(session_id)
        return tuple(state.segments) if state else ()

    def open_usage_segment(self, session_id: str) -> SandboxUsageSegment | None:
        """进行中的计费段（None = 无未封口段）。"""
        state = self._states.get(session_id)
        return state.open_segment if state else None

    # ---------------------------------------------------------------------- 内部

    def _require_state(self, session_id: str) -> _SessionState:
        state = self._states.get(session_id)
        if state is None:
            raise SandboxLifecycleError(f"会话 {session_id} 没有沙箱记录（先 create）")
        return state

    def _forward_output(self, session_id: str, chunk: SandboxOutputChunk) -> None:
        """沙箱输出块 → 活动计时 + 上游 sink（sandbox_output 事件源）。"""
        state = self._states.get(session_id)
        if state is not None:
            state.last_active_at = clock.utc_now()
        if self._on_output is not None:
            self._on_output(session_id, chunk)

    def _open_segment(self, state: _SessionState, phase: Literal["running", "paused"]) -> None:
        if state.open_segment is not None:
            self._close_segment(state)
        state.open_segment = SandboxUsageSegment(phase=phase, started_at=clock.utc_now())

    def _close_segment(self, state: _SessionState) -> None:
        segment = state.open_segment
        if segment is None:
            return
        ended_at = clock.utc_now()
        closed = replace(
            segment,
            ended_at=ended_at,
            duration_seconds=clock.elapsed_seconds(segment.started_at, ended_at),
        )
        state.open_segment = None
        record = SandboxUsageRecord(
            session_id=state.session_id,
            provider=state.provider,
            sandbox_id=state.sandbox_id or state.last_sandbox_id or "unknown",
            segment=closed,
        )
        state.segments.append(record)
        if self._on_usage_segment is not None:
            try:
                self._on_usage_segment(record)
            except Exception as exc:  # noqa: BLE001 —— 计费落库失败不拖垮生命周期
                self._log("sandbox_usage 分段回调失败（已忽略）", exc)

    def _emit(self, state: _SessionState, phase: SandboxPhase, *, reason: str | None) -> None:
        if self._on_lifecycle is None:
            return
        event = SandboxLifecycleEvent(
            session_id=state.session_id,
            phase=phase,
            provider=state.provider,
            sandbox_id=state.sandbox_id,
            reason=reason,
            at=clock.utc_now(),
        )
        try:
            self._on_lifecycle(event)
        except Exception as exc:  # noqa: BLE001 —— 事件出口失败不阻断相位机
            self._log("沙箱生命周期事件回调失败（已忽略）", exc)

    @staticmethod
    def _log(message: str, exc: Exception) -> None:
        log = BusinessLog(message=message, action="沙箱管理", entity="sandbox")
        log.warning(exc)


def _default_factory(provider: SandboxProvider, session_id: str) -> LcodeSandbox:
    """生产工厂：按全局配置造适配器（测试注入替身）。"""
    return create_sandbox_client(provider, session_id=session_id)


__all__ = [
    "LifecycleSink",
    "OutputSink",
    "RestoreCallback",
    "SandboxFactory",
    "SandboxLifecycleEvent",
    "SandboxManager",
    "SandboxRebuildResult",
    "SandboxUsageRecord",
    "UsageSink",
]
