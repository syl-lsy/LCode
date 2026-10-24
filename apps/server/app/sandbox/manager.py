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
- **懒加载**（bug 修复）：run 启动只经 `ensure_lazy` 登记**未 provision** 实例（零网络），
  真实 provision 推迟到首次实际使用（适配器 `_ensure_provisioned`）并经
  `_on_lazy_provision` 回调落相位/计费段/事件——纯聊天 run 全程零沙箱网络调用；
  未 provision 实例的 `release`/`shutdown`/`evaluate_idle`/`probe` 均零网络零误伤；
- **计费分段**（AC-015-4）：每次进入 running/paused 开段、离开时封口（时长取
  `clock.utc_now()` 差值，**金额估算归 cost_service**）；封口段经 `on_usage_segment`
  推给上层落 `sandbox_usage` 表，并留存在账本供查询；
- **关停策略**（U9）：`shutdown()` 对 running 沙箱显式 `pause()`（保留状态，重启可续）；
  会话 DELETE → `release()` 即 `kill()`（AC-002-3，幂等）。

时钟：全部取 `app.utils.clock.utc_now()`（模块属性调用时解析）——unit 层 patch 它即可
推进假时钟（test-plan §3.6）。本模块为同步实现：适配器与计费段都是廉价操作，真正的
异步边界在 run 执行层——run_service 用 `asyncio.to_thread` 把本模块的调用卸载到工作
线程（沙箱健壮性 S1），因此本模块必须自带线程安全（S2）：**会话级锁**（见 `ensure`
与 `_lock_for`）保证同一会话的相位决策互斥，不同会话互不阻塞。
"""

from __future__ import annotations

import threading
import weakref
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
    SandboxProvisionNotice,
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
        # S2 会话级锁：键 = session_id，值 = 该会话相位机的互斥锁。选 threading.RLock
        # （可重入）而非 Lock：公开相位方法在锁内会互相调用（shutdown→pause、
        # evaluate_idle→pause、probe→mark_crashed、ensure→resume/create），同线程重入
        # 不死锁；不同会话各持一把锁，互不阻塞。WeakValueDictionary 让无人引用的会话锁
        # 随 GC 回收（防长生命周期进程泄漏）；并发 get-or-create 由 `_locks_guard` 原子化
        # （否则两个线程可能各造一把锁，首个并发 ensure 的互斥失效）。
        self._locks: weakref.WeakValueDictionary[str, threading.RLock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = threading.Lock()

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
        with self._lock_for(session_id):
            client = self._live.get(session_id)
            if client is None:
                state = self._states.get(session_id)
                return SandboxStatus(
                    sandbox_id=state.last_sandbox_id if state else None,
                    phase=_stable(self.phase(session_id)),
                    available=False,
                    detail="无在册沙箱实例",
                )
            if not client.provisioned:
                # 懒加载未 provision：不是「不可用」，只是「还没用过」——如实回报（相位
                # 与账本对齐：通常是 none，provision 在途时为 creating），绝不标 crashed
                # （否则纯聊天会话会被一次健康探测打成崩溃态）。
                return SandboxStatus(
                    sandbox_id=None,
                    phase=_stable(self.phase(session_id)),
                    available=False,
                    detail="沙箱尚未实际使用（懒加载，未 provision）",
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
        """为会话 provision 一个沙箱（provider 未传 = 用当前全局配置并固化到账本）。

        「相位检查 → provision → 登记」全程持有该会话的锁（S2）：并发 create/ensure
        同一会话时串行化，重复创建的防御性检查在锁内判定才有意义（否则检查-创建窗口
        内第二个请求仍会穿过去）。
        """
        with self._lock_for(session_id):
            existing = self._states.get(session_id)
            if session_id in self._live or (
                existing is not None and existing.phase not in _TERMINAL
            ):
                raise SandboxLifecycleError(
                    f"会话 {session_id} 已有沙箱（phase={self.phase(session_id)}）；"
                    "重建请走 rebuild()"
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

    def ensure(
        self,
        session_id: str,
        *,
        provider: SandboxProvider | None = None,
        persisted_sandbox_id: str | None = None,
    ) -> tuple[LcodeSandbox, bool]:
        """会话沙箱就绪的**决策入口**（S1/S2/S3；run_service._ensure_backend 的落点）。

        锁的职责：在同一把会话锁内原子完成「检查-决策-登记」——`asyncio.to_thread` 把
        同步 provision 卸载到工作线程后（S1），同一会话的多个 run/恢复请求可能并发进入
        「查注册表 → 创建 → 登记」窗口；本方法持锁决策，保证一个会话**至多 provision
        一次**，后到者在锁上等待并复用先到者登记的实例。不同会话各持独立的锁（分片），
        互不阻塞。

        决策规则（S3 adopt 落地后的完整矩阵；**run 车道已改用懒语义 `ensure_lazy`**，
        本方法保留为 eager 就绪入口——adopt 失败转 rebuild 的完整 S3 语义在此）：
        - 在册（`get()` 非空）：`stable_phase == "paused"` 先 `resume`，否则直接返回实例；
        - 不在册且 `persisted_sandbox_id` 非空：**adopt 优先**——工厂新建适配器实例按
          持久 id 重连（上一进程生命周期留下的沙箱，文件系统仍在），成功即登记复用；
          失败（不存在/不可达）走既有 `rebuild()`（无 restore 回调 → 需重载现场）；
        - 不在册且无持久 id：走 `create`（none/killed/crashed 等终态可拉新；非终态重复
          创建仍被 create 的防御性检查拒绝）。

        返回 `(client, needs_reload)`：`needs_reload=True` 仅在 adopt 失败转重建时出现
        （新实例是空的，调用方应告知用户"需重载现场"；其余路径恒 False——重建告知的
        事件面已由 recreated 生命周期事件直达前端，本返回值是编程侧的冗余通道）。
        """
        with self._lock_for(session_id):
            client = self._live.get(session_id)
            if client is not None:
                if self.stable_phase(session_id) == "paused":
                    return self.resume(session_id), False  # RLock 可重入：同线程再加锁不死锁
                # 锁内刷新活跃计时（评审项 2 的 TOCTOU 根因修复）：ensure 释放锁到
                # run_service.begin_run（事件循环线程）之间存在窗口，sweeper 线程
                # （to_thread 跑 evaluate_idle）可能持锁评估空闲——不刷新的话
                # 「idle≥阈值 + active_runs 仍为 0」会让刚就绪的 run 被误 pause，
                # 首条命令即撞 SandboxUnavailable。resume/create/adopt 路径各自已在
                # 锁内刷新（resume 尾部 / create、_adopt_or_rebuild 尾部），此处补齐复用路径。
                self.touch(session_id)
                return client, False
            if persisted_sandbox_id:
                return self._adopt_or_rebuild(
                    session_id, provider=provider, sandbox_id=persisted_sandbox_id
                )
            return self.create(session_id, provider=provider), False

    def ensure_lazy(
        self,
        session_id: str,
        *,
        provider: SandboxProvider | None = None,
        persisted_sandbox_id: str | None = None,
    ) -> LcodeSandbox:
        """会话沙箱的**懒就绪**入口（run_service._ensure_backend 的落点；懒加载语义）。

        与 `ensure` 的差别：本方法**绝不触发网络**——构造并登记一个未 provision 的
        适配器实例（相位 none、零计费段、零生命周期事件），真实 provision 推迟到首次
        实际使用（`LcodeSandbox._ensure_provisioned`，即第一条命令/文件读写）时才由
        适配器内触发，经 provision observer 通知本 manager 做相位/计费段/事件迁移
        （`_on_lazy_provision`）。纯聊天 run 因此全程零沙箱网络调用。

        决策规则（会话锁内原子，S2 同 `ensure`）：
        - 在册（`get()` 非空）：直接返回实例——**paused 保持 paused**，不做网络 resume
          （resume 同样推迟到首次实际使用）；
        - 不在册：账本终态（none/killed/crashed）或未见会话 → 工厂造未 provision 实例
          并登记（复用既有账本保住计费分段历史与固化 provider）；有持久 id 时注入
          pending adopt id——首次使用走 `adopt` 而非 `create`，adopt 失败表现为工具错误
          并落 crashed（与 eager `ensure` 的 adopt 失败转 rebuild 不同：懒触发点已在
          工具调用内，由 deepagents 把异常转 ToolMessage，agent 可据错误自行决策）。
        """
        with self._lock_for(session_id):
            client = self._live.get(session_id)
            if client is not None:
                # 锁内刷新活跃计时（与 ensure 复用路径同口径；paused/none 相位下对
                # sweeper 无影响，纯属防御未来相位叠加）
                self.touch(session_id)
                return client
            existing = self._states.get(session_id)
            if existing is not None and existing.phase not in _TERMINAL:
                raise SandboxLifecycleError(
                    f"会话 {session_id} 有账本但无在册实例（phase={existing.phase}）；"
                    "非终态相位不允许重新登记（懒加载）"
                )
            resolved = (
                existing.provider
                if existing is not None
                else provider or cast("SandboxProvider", sandbox_settings.provider)
            )
            client = self._factory(resolved, session_id)
            if persisted_sandbox_id:
                client.set_pending_adopt_id(persisted_sandbox_id)
            # 闭包绑定实例身份：迟到通知（实例已被替换/释放）按身份判伪丢弃
            client.set_provision_observer(
                lambda notice: self._on_lazy_provision(session_id, client, notice)
            )
            client.add_output_handler(lambda chunk: self._forward_output(session_id, chunk))
            state = existing or _SessionState(session_id=session_id, provider=resolved)
            state.phase = "none"
            # P2-2：重登记（crashed/killed 复用账本）时清空当前实例 id——账本可能残留
            # 上一实例的 sandbox_id（如 provision 后 crashed），不带清会让 creating
            # 生命周期事件携带陈旧 id 上 wire（诚实口径：creating 尚无可引用身份）；
            # `last_sandbox_id` 保留供 usage 对账，不受影响。
            state.sandbox_id = None
            self._states[session_id] = state
            self._live[session_id] = client
            state.last_active_at = clock.utc_now()
            return client

    def _on_lazy_provision(
        self, session_id: str, client: LcodeSandbox, notice: SandboxProvisionNotice
    ) -> None:
        """懒 provision 通知回调（**工具执行线程内**触发；持会话锁迁移，S2）。

        这是懒加载与既有相位机/计费段/生命周期事件的唯一汇合点——成功与失败两侧的
        迁移都复用 eager 路径的语义（running/resumed/crashed + 分段开闭 + `_emit`），
        契约事件相位枚举零变更。

        - `creating`：provision 开始 → 相位 creating（emit；sandbox_id 为 None，桥接层
          按契约过滤，不进 wire 面）；
        - `running`：create/adopt 成功 → 相位 running、登记 id、开 running 计费段、
          emit running（adopt 成功带 reason="adopt"，与 eager 同口径）；
        - `resumed`：paused 实例懒 resume 成功 → 封 paused 段、开 running 段、
          emit resumed（reason="user"，与 eager resume 同口径）；
        - `crashed`：provision 失败 → `mark_crashed` 语义（摘表/封段/emit crashed）；
          此刻 manager 侧通常还没有 running 段（段只在成功通知后开），`mark_crashed`
          的 `_close_segment` 对无段是 no-op——不会留下悬挂计费段。

        身份校验：`_live` 当前实例必须是通知方——实例被 release 摘除或 replace 后，
        迟到通知与当前会话状态无关，直接丢弃（绝不把旧实例的失败算在新实例头上）。
        """
        with self._lock_for(session_id):
            state = self._states.get(session_id)
            if state is None:
                return
            if self._live.get(session_id) is not client:
                # 实例已被替换/释放（迟到通知作废）。若迟到的是 provision 成功通知，
                # provider 侧已多出一个无人认领的活沙箱——这是它能被杀掉的第一个时刻
                # （release「零网络摘除」与在途 provision 的窄竞态收口，P2-1）：
                # best-effort kill，失败只记日志（通知是旁路，绝不反噬工具线程）。
                if notice.phase == "running" and client.sandbox_id is not None:
                    self._kill_orphan(client)
                return
            if notice.phase == "creating":
                state.phase = "creating"
                self._emit(state, "creating", reason=None)
            elif notice.phase == "running":
                state.phase = "running"
                state.sandbox_id = client.sandbox_id
                state.last_sandbox_id = client.sandbox_id
                state.last_active_at = clock.utc_now()
                self._open_segment(state, "running")
                self._emit(state, "running", reason=notice.reason)
            elif notice.phase == "resumed":
                self._close_segment(state)
                self._open_segment(state, "running")
                state.phase = "resumed"
                state.last_active_at = clock.utc_now()
                self._emit(state, "resumed", reason=notice.reason or "user")
            else:  # crashed
                self.mark_crashed(session_id, reason=notice.reason or "懒 provision 失败")

    def _adopt_or_rebuild(
        self, session_id: str, *, provider: SandboxProvider | None, sandbox_id: str
    ) -> tuple[LcodeSandbox, bool]:
        """按持久 id 重连既有沙箱；失败转 `rebuild`（**调用方必须已持该会话锁**）。

        账本原则：既有 `_SessionState` 一律复用（保住计费分段历史与固化的 provider），
        只有无账本时（进程重启后的首见会话）才新建；`phase` 先落 `crashed` 是
        `rebuild()` 的前置条件（adopt 尝试期间无可观测的注册表状态——实例从未在册）。
        """
        existing = self._states.get(session_id)
        if existing is not None:
            resolved = existing.provider  # AC-002-6：账本固化值优先于调用方传参
            existing.phase = "crashed"  # rebuild 的前置；crashed → recreated 由 rebuild 发事件
            if existing.last_sandbox_id is None:
                existing.last_sandbox_id = sandbox_id
            state = existing
        else:
            resolved = provider or cast("SandboxProvider", sandbox_settings.provider)
            state = _SessionState(
                session_id=session_id,
                provider=resolved,
                phase="crashed",
                last_sandbox_id=sandbox_id,
            )
            self._states[session_id] = state

        client = self._factory(resolved, session_id)
        try:
            client.adopt(sandbox_id)
        except SandboxError:
            # 持久 id 已失效（沙箱被回收/不存在）：按 AC-002-4 既有重建路径收口——
            # 无 restore 回调 → needs_reload=True（"明确告知需重载"），recreated 事件
            # 携带新 id 经事件桥写回 sessions.sandbox_id（下次重启 adopt 到活实例）。
            result = self.rebuild(session_id, reason=f"adopt 失败（{sandbox_id}），已重建")
            return result.sandbox, result.needs_reload

        client.add_output_handler(lambda chunk: self._forward_output(session_id, chunk))
        self._live[session_id] = client
        state.phase = "running"
        state.sandbox_id = client.id
        state.last_sandbox_id = client.id
        state.last_active_at = clock.utc_now()
        self._open_segment(state, "running")
        self._emit(state, "running", reason="adopt")
        return client, False

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

        锁纪律（S2/S3）：命令执行本身**不持会话锁**——时长不可控，锁内执行会把同一会话的
        ensure/pause 全部堵住；其失败迁移经 `mark_crashed` 的会话锁串行化，与相位决策
        不冲突。`touch`/`begin_run`/`end_run` 亦无锁：写者只有事件循环线程（串行无丢
        更新），而 pause 决策依赖的 `last_active_at` 在每条「run 即将使用实例」的 ensure
        路径上都已在会话锁内刷新——空闲评估不可能对刚就绪的 run 看到陈旧空闲时长
        （TOCTOU 根因已在 ensure 处关死；begin_run/end_run 加锁反而会让 sweeper 的
        pause 网络调用阻塞事件循环，见 begin_run docstring）。
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
        """run 开始：活跃计数 +1（期间空闲 pause 被抑制）。

        无锁约定（评审项 2 复核结论）：写者只有事件循环线程（单线程串行，无丢更新），
        且 pause 决策依赖的另一字段 `last_active_at` 在每条「run 即将使用实例」的 ensure
        路径上都已在会话锁内刷新——若这里也加锁，sweeper 线程持锁做 pause（秒级网络
        调用）时会阻塞事件循环，违背 S1 的卸载目标。
        """
        state = self._require_state(session_id)
        state.active_runs += 1
        state.last_active_at = clock.utc_now()

    def end_run(self, session_id: str) -> None:
        """run 结束：活跃计数 -1 并重置空闲计时（刚结束不算空闲）。"""
        state = self._require_state(session_id)
        # 写序钉死（先重置计时再递减计数）：sweeper 在锁外读这两个字段，任何中间态都必须
        # 安全——先递减会出现「计数已为 0 但计时仍是旧值」的瞬读，run 时长超过空闲阈值时
        # 会被立刻误判空闲；先重置则中间态恒为「计数≥1」（不 pause）或「计数 0 + 新计时」。
        state.last_active_at = clock.utc_now()
        state.active_runs = max(state.active_runs - 1, 0)

    def active_runs(self, session_id: str) -> int:
        """该会话正在跑的 run 数（0 = 可被空闲 pause）。"""
        return self._require_state(session_id).active_runs

    # ------------------------------------------------------------------ 生命周期

    def pause(self, session_id: str, *, reason: str | None = None) -> None:
        """显式 pause（AC-002-2）：running/resumed/recreated → paused，计费段随之切换。

        相位迁移持会话锁（S2）：与 ensure/rebuild 等并发时，迁移判定与迁移动作原子。
        """
        with self._lock_for(session_id):
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
        """从 paused 恢复（沿用同一实例，不重建；AC-002-2）。相位迁移持会话锁（S2）。"""
        with self._lock_for(session_id):
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

        相位迁移持会话锁（S2）：与 ensure 并发时「kill-摘表」对后者原子——ensure 要么
        在 release 前拿到存活实例，要么在 release 后看到不在册而走 create，不会拿到
        正在被 kill 的实例。
        """
        with self._lock_for(session_id):
            state = self._states.get(session_id)
            if state is None:
                return
            pending = self._pending_kill.get(session_id)
            client = pending if pending is not None else self._live.get(session_id)
            if client is None:
                return
            if client.sandbox_id is None:
                # 懒加载未 provision：此刻 provider 侧没有可 kill 的实例、也没有计费段
                # 可封（懒实例从未开段）——只移注册表并把账本落终态。
                # 窄竞态如实说明（P2-1）：若工具线程的懒 provision 恰在本摘除之后完成，
                # 迟到的 running 通知会由 `_on_lazy_provision` 的身份不符分支 best-effort
                # kill 收口；本分支不做等待/加锁（DELETE 请求不得被工具线程阻塞）。
                self._pending_kill.pop(session_id, None)
                if self._live.get(session_id) is client:
                    del self._live[session_id]
                self._close_segment(state)
                state.phase = "killed"
                self._emit(state, "killed", reason=reason)
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
            # P3-② 兜底摘除：kill 成功即代表会话已释放，`_live` 里不应再有任何在册实例
            # ——两次 release 之间新登记的另一实例（懒加载重登记引入的 pending 与 live
            # 共存面）也一并摘除；与被 kill 实例不同的 live 实例补一次 best-effort kill。
            live = self._live.get(session_id)
            if live is not None:
                if live is not client:
                    self._kill_orphan(live)
                del self._live[session_id]
            self._close_segment(state)
            state.phase = "killed"
            self._emit(state, "killed", reason=reason)

    def mark_crashed(self, session_id: str, *, reason: str) -> None:
        """标记意外不可用（幂等）：移出注册表、封口计费段、run 计数清零（AC-002-4）。

        相位迁移持会话锁（S2）；pause/resume/run_command 的失败路径经此收敛。
        """
        with self._lock_for(session_id):
            state = self._require_state(session_id)
            if state.phase in {"crashed", "killed"}:
                return
            client = self._live.pop(session_id, None)
            if client is not None and client.sandbox_id is not None:
                # 未 provision（懒加载）实例的 id 是占位值，不得污染 last_sandbox_id
                state.last_sandbox_id = client.sandbox_id
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
        """重建 crashed 沙箱（AC-002-4）：provider 沿用账本固化值，可带恢复回调。

        相位迁移持会话锁（S2）：与 ensure 并发时，「crashed 判定 → provision → 登记」
        原子——ensure 不会在重建中途看到半成品。
        """
        with self._lock_for(session_id):
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
        """服务进程关停：对运行态沙箱显式 pause（保状态可续，U9），不 kill。

        多会话遍历：先对注册表取**快照**再逐会话持锁处理（锁内重读账本——会话可能在
        快照后被并发 release/mark_crashed 摘除，重读后干净跳过）；单会话失败不阻断
        其余关停。
        """
        for session_id in tuple(self._live):
            with self._lock_for(session_id):
                state = self._states.get(session_id)
                if state is None or state.phase not in _RUNNING_LIKE:
                    continue
                try:
                    self.pause(session_id, reason="shutdown")
                except SandboxError as exc:  # 单个会话失败不阻断其余关停（已标 crashed）
                    self._log(f"关停时 pause 失败（session={session_id}）", exc)

    def evaluate_idle(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """空闲检查（lifespan 周期调用）：到阈值且无活跃 run 才 pause，返回被暂停的会话。

        run 活跃期间**永不 pause**（Q3）；刚 end_run 的会话因计时被重置而不会立即暂停。
        多会话遍历同 shutdown：快照 + 逐会话锁内重读（并发的 create/release 不会让
        本方法撞上「字典迭代中改变大小」，也不会对刚被释放的会话误 pause）。
        """
        current = now or clock.utc_now()
        paused: list[str] = []
        for session_id, _ in tuple(self._states.items()):
            with self._lock_for(session_id):
                state = self._states.get(session_id)
                if state is None or state.phase not in _RUNNING_LIKE:
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

    def _lock_for(self, session_id: str) -> threading.RLock:
        """取该会话的相位机锁（get-or-create 在 guard 内原子完成，S2）。

        原子性是互斥正确性的前提：两个线程同时 ensure 一个**全新**会话时，若各造一把
        锁则互斥失效（并发重复创建回归）。guard 只包住「查表-造锁-登记」三步，锁的持有
        期由调用方的 `with` 控制。锁值用 RLock（可重入）——公开相位方法在锁内互相调用
        （ensure→resume/create、shutdown/evaluate_idle→pause、probe→mark_crashed），
        同线程重入不死锁；锁粒度恒为**单会话**，任何方法都不同时持有两把会话锁
        （shutdown/evaluate_idle 逐会话处理，处理完一把再取下一把），无锁序死锁面。
        """
        lock = self._locks.get(session_id)
        if lock is not None:
            return lock
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock

    def _require_state(self, session_id: str) -> _SessionState:
        state = self._states.get(session_id)
        if state is None:
            raise SandboxLifecycleError(f"会话 {session_id} 没有沙箱记录（先 create）")
        return state

    @staticmethod
    def _kill_orphan(client: LcodeSandbox) -> None:
        """无人认领实例的 best-effort kill（P2-1 泄漏收口；失败只记日志不抛）。"""
        try:
            client.kill()
        except Exception as exc:  # noqa: BLE001 —— 收口失败不能反噬通知主链
            SandboxManager._log("泄漏沙箱实例 kill 失败（请人工核查 provider 侧）", exc)

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
