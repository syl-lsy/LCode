"""`LcodeSandbox`：deepagents `BaseSandbox` 之上的 LCode 扩展层（A3 §4.1/§4.2）。

继承关系与职责分层（A3 §4.1）：

```
BaseSandbox（deepagents 0.6.12，backends/sandbox.py）
     ▲  文件工具语义（ls/read/write/edit/delete/grep/glob）由基类经命令执行派生，
     │  LCode **不重写**——只翻译执行与文件读写原语
LcodeSandbox（本模块）
     ▲  ① 协议 4 抽象成员的具体实现：execute / upload_files / download_files / id
     │  ② LCode 扩展（协议外）：生命周期 create/adopt/pause/resume/kill/status、
     │     输出流回调、usage 分段钩子、round-0 兜底基线、工具链探针
     ├── BailianSandbox（bailian.py，e2b==2.31.0 钉死 / E2B 协议）
     └── OpenSandboxSandbox（opensandbox.py，自有协议 / 1.1.1）
```

四个抽象成员的落法（深读安装版 `backends/sandbox.py` 后的结论：`BaseSandbox` 只要求
`execute` / `upload_files` / `download_files` / `id`）：

- `execute(command, *, timeout, exec_id)`：协议成员，**委托** `run_command()`；
  `exec_id` 是 LCode 追加的关键字参数（默认 None，协议调用不受影响）——工具层把它设成
  `tool_call_id`，输出块据此在终端标注来源（AC-002-1）；
- `run_command()`：LCode 命名的单命令执行入口（manager / 基线助手 / 工具层都用它），
  实现落在各 provider 的 `_execute_command()` 原语上；
- `upload_files` / `download_files`：批量语义（逐文件部分成功、响应顺序与入参一致）与
  错误归一化（`FileOperationError` 四值）在本层完成，provider 只实现 `_write_file` /
  `_read_file` 单文件原语——适配器保持薄翻译（AGENTS.md 接缝表）；
- `id`：实例属性（未 provision 时给可读占位，便于日志定位）。

生命周期（create/pause/resume/kill/status）是**协议外扩展**：deepagents 假设沙箱已活着，
故这些方法不进入对齐面，由 `manager.py` 编排（A3 §4.3）。sandbox 层只 raise 本模块的
异常族；到 REST 的映射由上层 service 负责（`sandbox/` 不依赖 `events/`、`models/`）。

**懒 provision（懒加载）**：三个协议原语入口（`run_command` / `upload_files` /
`download_files`）先经 `_ensure_provisioned()` 确保实例可用——首次实际使用才触发
create/adopt/resume 网络调用，并经 `ProvisionObserver` 通知 manager 做相位/计费段/
生命周期事件迁移；纯聊天 run 全程零网络、零计费段。provision 失败表现为该次工具
调用的工具错误（异常经原语上抛），不炸整个 run。

密钥纪律（NFR-004）：本模块只出现「环境变量名」，不出现密钥本体；读取发生在
`bailian.py` / `opensandbox.py` 的 `_require_api_key()`。
"""

from __future__ import annotations

import abc
import re
import shlex
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from app.core.logger import BusinessLog
from app.utils.exec_context import EXEC_ID_CONTEXT
from lcode_contracts.baseline import BaselineCommitMessage, check_commit_sha, tag_for_round

# --------------------------------------------------------------------------------- 类型

SandboxProvider = Literal["bailian", "opensandbox"]
"""沙箱 provider 枚举（契约 `SandboxProvider` 同口径；AC-002-6）。"""

SandboxPhase = Literal[
    "none", "creating", "running", "paused", "resumed", "killed", "crashed", "recreated"
]
"""事件侧相位（契约 `SandboxOutputEvent.phase` 七值 + none）。

`resumed` / `recreated` 是**迁移动作**（不是稳态）：查询侧用
`stable_phase()` 把它们折叠成 `running`（契约 `SandboxStatus.phase` 的既定口径）。
"""

StableSandboxPhase = Literal["none", "creating", "running", "paused", "killed", "crashed"]
"""查询侧相位（契约 `SandboxStatus.phase`：动作折叠后的稳态）。"""

StreamName = Literal["stdout", "stderr"]
"""输出流名（契约 `SandboxOutputEvent.channel` 的输出侧两值）。"""

UsagePhase = Literal["running", "paused"]
"""计费分段相位（契约 `sandbox_usage.phase` 两值，AC-015-4）。"""


# --------------------------------------------------------------------------------- 异常


class SandboxError(Exception):
    """沙箱层异常基类（`message` 面向使用方；`detail` 供排查，不进事件/响应）。

    与 REST 的关系：沙箱失败在工具面表现为 `ToolErrorCode.sandbox_unavailable`
    （由 `app/utils/error_classifier.py` 分类，M2 任务 5 落点），**不**注册进
    `exception/handler/errors.py` 的 ERROR_MAP——沙箱异常不直出 HTTP 面。

    字段口径对齐基座 `BusinessException`：`str(exc)` 只给 message，`detail` /
    `original_exception` 留给日志与排查（`detail` 由适配器截断，且绝不含密钥）。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        original_exception: Exception | None = None,
    ) -> None:
        self.message = message
        self.detail = detail
        self.original_exception = original_exception
        super().__init__(message)


class SandboxConfigurationError(SandboxError):
    """配置/凭证缺失（非重试类）：缺 `*_API_KEY_ENV` 指向的环境变量、未知 provider。

    消息只含**环境变量名或 provider 名**，绝不回显密钥值（NFR-004）。
    """


class SandboxUnavailable(SandboxError):
    """沙箱意外不可用（SDK 故障 / 远端回收 / 执行超时）：可重建（AC-002-4）。"""


class SandboxLifecycleError(SandboxError):
    """相位机非法迁移（重复 create、未 pause 就 resume、crashed 上 resume……）。

    属编程错误而非环境故障，故不触发重建路径。
    """


class SandboxFileError(SandboxError):
    """单文件读写的归一化错误（provider 原语抛，批量层转 `FileOperationError`）。"""

    def __init__(self, kind: str, path: str, *, detail: str = "") -> None:
        super().__init__(f"沙箱文件操作失败（{kind}）：{path}", detail=detail)
        self.kind = kind
        self.path = path


# --------------------------------------------------------------------------------- 数据


@dataclass(frozen=True, slots=True)
class SandboxOutputChunk:
    """一条命令输出块（`sandbox_output` 事件的源数据；AC-002-1）。

    `exec_id` 关联发起命令的工具调用（契约：stdout/stderr 通道必须带 exec_id），
    非工具路径（文件工具派生命令）为 None。
    """

    stream: StreamName
    data: str
    exec_id: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    """provider 侧活性探测结果（`status()` 的返回；不抛错，不可用即如实回报）。"""

    sandbox_id: str | None
    phase: StableSandboxPhase = "none"
    available: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SandboxUsageSegment:
    """计费分段快照（一行 = 一个 running/paused 段，AC-015-4）。

    `duration_seconds` 只由**时长**决定；金额估算归 `cost_service`（费率口径
    SANDBOX_COST_RATE_PER_HOUR / SANDBOX_PAUSED_RATE_PER_GIB_HOUR），本层不猜金额。
    """

    phase: UsagePhase
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    currency: str = "CNY"

    @property
    def open(self) -> bool:
        """True = 该段仍在进行（尚未封口）。"""
        return self.ended_at is None


@dataclass(frozen=True, slots=True)
class BaselineInit:
    """round-0 兜底基线的产出（非 git 输入分支；AC-002-5）。"""

    commit: str
    tag: str
    message: str


@dataclass(frozen=True, slots=True)
class CommandProbe:
    """一条工具链探针的结果（Q7：镜像是否预装 git/python3/coreutils）。"""

    command: str
    exit_code: int | None
    output: str

    @property
    def present(self) -> bool:
        return self.exit_code == 0


OutputHandler = Callable[[SandboxOutputChunk], None]
UsageHandler = Callable[[SandboxUsageSegment], None]

ProvisionPhase = Literal["creating", "running", "resumed", "crashed"]
"""懒 provision 通知相位（适配器 → manager；manager 据此做相位/计费段/事件迁移）。"""


@dataclass(frozen=True, slots=True)
class SandboxProvisionNotice:
    """一次懒 provision 的状态通知（在**工具执行线程**内发出，非事件循环线程）。

    - `creating`：provision 网络动作开始前（manager 落 creating 相位并 emit）；
    - `running`：create/adopt 成功（manager 落 running、开计费段、emit）；
    - `resumed`：paused 实例的懒 resume 成功（manager 切段并 emit resumed）；
    - `crashed`：provision 失败（manager 走 mark_crashed 语义：摘表、封段、emit）。
    """

    phase: ProvisionPhase
    reason: str | None = None


ProvisionObserver = Callable[[SandboxProvisionNotice], None]
"""懒 provision 观察者（实现 = `SandboxManager._on_lazy_provision`；只通报不决策）。"""

# round-0 基线用的 git 命令与身份（沙箱内可能没有 git 身份配置，显式 -c 传入，
# 不修改沙箱内的 git config——AC-002-5 要求"不做任何 git 写操作"仅限于 git 输入分支）
GIT_PROBE_COMMAND = "git rev-parse --is-inside-work-tree"
BASELINE_TIMEOUT_SECONDS = 120
GIT_IDENTITY_NAME = "LCode Baseline"
GIT_IDENTITY_EMAIL = "lcode-baseline@localhost"

COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")

TOOLCHAIN_PROBES: tuple[tuple[str, str], ...] = (
    ("git", "git --version"),
    ("python3", "python3 --version"),
    ("coreutils", "ls --version | head -n 1"),
)
"""Q7 探针三件套：BaseSandbox 的文件工具依赖 POSIX sh + coreutils + python3，
diff 链与 round-0 基线依赖 git（缺 git 则模版构建阶段必须装）。"""


def combine_output(stdout: str, stderr: str) -> str:
    """把两条流拼成 `ExecuteResponse.output`（deepagents 口径：合并输出）。

    顺序固定 stdout → stderr（不含分隔符噪声）：同一命令两次调用结果稳定可比。
    """
    return f"{stdout}{stderr}"


# --------------------------------------------------------------------------------- 基类


class LcodeSandbox(BaseSandbox, abc.ABC):
    """沙箱适配器基类：协议 4 成员的具体实现 + LCode 扩展面。

    子类必须实现的**原语**（比 deepagents 抽象面更薄，便于 provider 薄翻译）：

    - `_execute_command(command, *, timeout, exec_id)`：跑一条命令，输出块经
      `emit_output_text()` 分发；
    - `_write_file(path, content)` / `_read_file(path)`：单文件读写（失败抛
      `SandboxFileError`）；
    - `create` / `adopt` / `pause` / `resume` / `kill` / `status`：生命周期（协议外扩展）。

    provider 标识默认在类上声明、实例可覆盖（factory 按它注册；AC-002-6）。
    """

    provider: SandboxProvider = "bailian"
    """provider 标识：类属性给默认值，实例可覆盖（AC-002-6；factory 按它注册）。"""

    def __init__(self, *, session_id: str | None = None) -> None:
        self.session_id = session_id
        self._sandbox_id: str | None = None
        self._output_handlers: list[OutputHandler] = []
        self._usage_handlers: list[UsageHandler] = []
        # ---- 懒 provision（懒加载）状态面 ----
        self._provision_observer: ProvisionObserver | None = None
        self._pending_adopt_id: str | None = None
        self._paused = False
        self._provision_error: SandboxError | None = None
        self._provision_lock = threading.Lock()

    # ------------------------------------------------------- 懒 provision（懒加载）

    @property
    def provisioned(self) -> bool:
        """是否已 provision（持有真实 provider 侧实例与 id）；懒加载/短路判据。"""
        return self._sandbox_id is not None

    @property
    def paused(self) -> bool:
        """适配器侧 pause 标志（懒 resume 判据；由 pause()/resume()/create()/adopt() 维护）。"""
        return self._paused

    def set_provision_observer(self, observer: ProvisionObserver | None) -> None:
        """注册懒 provision 通知回调（manager 注入；触发点在工具执行线程内）。"""
        self._provision_observer = observer

    def set_pending_adopt_id(self, sandbox_id: str) -> None:
        """注入懒 provision 的 adopt 目标（`sessions.sandbox_id` 持久值；manager 注入）。

        只在**未 provision** 时有意义：首次实际使用时走 `adopt` 而非 `create`。
        """
        self._pending_adopt_id = sandbox_id

    def _mark_paused(self) -> None:
        """生命周期 pause 成功后登记（`_ensure_provisioned` 的 resume 判据）。"""
        self._paused = True

    def _mark_resumed(self) -> None:
        """生命周期 create/adopt/resume 成功后登记（实例处于可执行状态）。"""
        self._paused = False

    def _ensure_provisioned(self) -> None:
        """懒 provision：**首次实际使用**沙箱时拉起（run_command/upload_files/download_files
        入口）。

        幂等：已 provision 且非 paused 直接返回；provision 失败**烙死**——同实例后续
        调用重复抛同一错误，绝不在 manager 已摘除该实例后另起一个无人认领的沙箱。

        路径优先级：未 provision + pending adopt id → `adopt`；已 provision + paused →
        `resume`；否则 → `create`。adopt 遇「不可达」类失败（`SandboxUnavailable`，如
        持久 id 已被回收）时在同一次首次使用内降级 create 兜底（成功通知 reason 改
        "recreated"，对齐 eager adopt→rebuild 自愈链，AC-002-4）；配置类错误不降级。
        每个网络动作前后经 provision observer 通知 manager 做相位/计费段/生命周期事件
        迁移（`SandboxManager._on_lazy_provision`），失败经同通道告知（manager 落
        crashed），异常原样上抛——由 deepagents 工具层转 ToolMessage，不炸整个 run。
        """
        with self._provision_lock:
            if self._provision_error is not None:
                raise self._provision_error
            if self.provisioned and not self._paused:
                return
            observer = self._provision_observer
            try:
                if not self.provisioned:
                    pending = self._pending_adopt_id
                    self._pending_adopt_id = None  # 只消费一次（失败即死，不重放）
                    self._notify_provision(observer, SandboxProvisionNotice("creating", None))
                    if pending is not None:
                        try:
                            self.adopt(pending)
                        except SandboxUnavailable:
                            # P1 自愈：持久 id 已失效（被回收/不存在）→ 同一次首次使用内
                            # 降级 create 兜底（对齐 eager adopt→rebuild 自愈链，AC-002-4）。
                            # 成功通知 reason 改 "recreated"（保留重建告知语义）；降级只
                            # 针对「不可达」类，配置类错误仍按失败收敛（create 同样会撞）。
                            # 烙死交互：create 兜底也失败时异常上抛到外层 except——
                            # `_provision_error` 只对最终失败生效。
                            self.create()
                            self._mark_resumed()
                            self._notify_provision(
                                observer, SandboxProvisionNotice("running", "recreated")
                            )
                            return
                        self._mark_resumed()
                        self._notify_provision(observer, SandboxProvisionNotice("running", "adopt"))
                    else:
                        self.create()
                        self._mark_resumed()
                        self._notify_provision(observer, SandboxProvisionNotice("running", None))
                    return
                # 已 provision 但 paused：先恢复再使用（沿用同一 handle，不重建）
                self.resume()
                self._mark_resumed()
                self._notify_provision(observer, SandboxProvisionNotice("resumed", "user"))
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, SandboxError)
                    else SandboxUnavailable(
                        f"懒 provision 失败：{type(exc).__name__}",
                        detail=str(exc),
                        original_exception=exc,
                    )
                )
                self._provision_error = error
                self._notify_provision(observer, SandboxProvisionNotice("crashed", error.message))
                raise error from (None if error is exc else exc)

    @staticmethod
    def _notify_provision(
        observer: ProvisionObserver | None, notice: SandboxProvisionNotice
    ) -> None:
        """provision 通知的旁路出口：observer 故障只记日志，不阻断命令/错误主链。"""
        if observer is None:
            return
        try:
            observer(notice)
        except Exception as exc:  # noqa: BLE001 —— 通知是旁路，坏了不能拖垮命令
            log = BusinessLog(
                message="懒 provision 通知回调失败（已忽略，不影响沙箱使用）",
                action="沙箱回调",
                entity="sandbox",
            )
            log.warning(exc)

    # ------------------------------------------------------- 协议成员（deepagents 面）

    @property
    def id(self) -> str:
        """沙箱唯一标识（协议成员）。未 provision 时返回可读占位，便于日志定位。"""
        return self._sandbox_id or f"{self.provider}:pending:{self.session_id or 'anonymous'}"

    @property
    def sandbox_id(self) -> str | None:
        """已 provision 的真实沙箱 id（未 provision = None；写 `sandbox_usage` 用）。"""
        return self._sandbox_id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        exec_id: str | None = None,
    ) -> ExecuteResponse:
        """deepagents `BaseSandbox` 抽象成员：执行一条命令。

        `exec_id` 是 LCode 追加的**关键字参数**（协议调用不带它，语义不变）：工具层传
        `tool_call_id`，输出块据此在终端标注来源（AC-002-1）。
        """
        return self.run_command(command, timeout=timeout, exec_id=exec_id)

    def run_command(
        self,
        command: str,
        *,
        timeout: int | None = None,
        exec_id: str | None = None,
    ) -> ExecuteResponse:
        """LCode 命名的单命令执行入口（manager / 基线助手 / 工具层统一走这里）。

        `exec_id=None` 时回退 `EXEC_ID_CONTEXT`（tap 中间件在工具调用期间写入的
        tool_call_id）：deepagents 0.6.12 的 execute/文件工具不向 backend 传该扩展
        参数，输出块据此标注来源（AC-002-1）；上下文也没有 = 无工具路径，块由上游
        事件桥按契约诚实跳过。
        """
        self._ensure_provisioned()  # 懒加载：首次实际使用才 provision（纯聊天不触网）
        if exec_id is None:
            exec_id = EXEC_ID_CONTEXT.get()
        return self._execute_command(command, timeout=timeout, exec_id=exec_id)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """批量上传：逐文件部分成功，响应顺序与入参一致（deepagents 契约）。

        单文件失败不影响其余文件；已知条件归一化成 `FileOperationError` 四值
        （file_not_found / permission_denied / is_directory / invalid_path），
        未知异常如实带上类型名（deepagents 允许 backend-specific 错误串）。
        """
        self._ensure_provisioned()  # 懒加载：首次实际使用才 provision（纯聊天不触网）
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                self._write_file(path, content)
            except SandboxFileError as exc:
                responses.append(FileUploadResponse(path=path, error=exc.kind))
            except Exception as exc:  # noqa: BLE001 —— 逐文件兜底，不因一个文件炸掉整批
                responses.append(FileUploadResponse(path=path, error=_describe_error(exc)))
            else:
                responses.append(FileUploadResponse(path=path))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """批量下载：逐文件部分成功，响应顺序与入参一致（deepagents 契约）。"""
        self._ensure_provisioned()  # 懒加载：首次实际使用才 provision（纯聊天不触网）
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = self._read_file(path)
            except SandboxFileError as exc:
                responses.append(FileDownloadResponse(path=path, content=None, error=exc.kind))
            except Exception as exc:  # noqa: BLE001 —— 见 upload_files 同款兜底
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=_describe_error(exc))
                )
            else:
                responses.append(FileDownloadResponse(path=path, content=content))
        return responses

    # ------------------------------------------------------- LCode 扩展（协议外，§4.2）

    @abc.abstractmethod
    def _execute_command(
        self, command: str, *, timeout: int | None, exec_id: str | None
    ) -> ExecuteResponse:
        """原语：执行单条命令（provider SDK 翻译 + 输出块分发）。"""

    @abc.abstractmethod
    def _write_file(self, path: str, content: bytes) -> None:
        """原语：写单文件（父目录不存在时一并创建；失败抛 `SandboxFileError`）。"""

    @abc.abstractmethod
    def _read_file(self, path: str) -> bytes:
        """原语：读单文件为字节（失败抛 `SandboxFileError`）。"""

    @abc.abstractmethod
    def create(self, *, timeout_seconds: int | None = None) -> None:
        """provision 沙箱实例（相位 creating 的实体动作；失败抛 `SandboxUnavailable`）。"""

    @abc.abstractmethod
    def adopt(self, sandbox_id: str) -> None:
        """按 provider 侧**已存在**的沙箱 id 重连并取得 handle（S3 adopt）。

        语义：不做任何 provision——id 来自上一进程生命周期留下的 `sessions.sandbox_id`，
        provider 侧实例可能仍在（paused 自动恢复、running 直接连上）。成功后实例的
        `id`/`sandbox_id` 即 `sandbox_id`；沙箱不存在/不可达抛 `SandboxUnavailable`
        （调用方转重建路径），配置缺失抛 `SandboxConfigurationError`。只能作用于尚未
        provision 的适配器实例（先 adopt 或先 create，二者互斥）。
        """

    @abc.abstractmethod
    def pause(self) -> None:
        """显式 pause（百炼口径：**只有显式调用才保留状态**，硬约束 8；AC-002-2）。"""

    @abc.abstractmethod
    def resume(self) -> None:
        """从 paused 恢复（沿用同一实例的 handle，不重建）。"""

    @abc.abstractmethod
    def kill(self) -> None:
        """终止并释放沙箱（会话 DELETE 路径；AC-002-3）。"""

    @abc.abstractmethod
    def status(self) -> SandboxStatus:
        """探测 provider 侧状态（**不抛错**：不可用即 `available=False` + detail）。"""

    # ------------------------------------------------------------------ 输出流回调

    def add_output_handler(self, handler: OutputHandler) -> None:
        """注册输出回调（manager 用它把输出块转发成 `sandbox_output` 事件）。"""
        if handler not in self._output_handlers:
            self._output_handlers.append(handler)

    def remove_output_handler(self, handler: OutputHandler) -> None:
        """注销输出回调（幂等：不存在时不报错）。"""
        if handler in self._output_handlers:
            self._output_handlers.remove(handler)

    @property
    def output_handlers(self) -> tuple[OutputHandler, ...]:
        """当前已注册的输出回调（只读快照，测试与诊断用）。"""
        return tuple(self._output_handlers)

    def emit_output(self, chunk: SandboxOutputChunk) -> None:
        """分发一个输出块（空数据块直接丢弃：空事件在终端上没有意义）。"""
        if not chunk.data:
            return
        for handler in self._output_handlers:
            try:
                handler(chunk)
            except Exception as exc:  # noqa: BLE001 —— 回调是旁路，坏了不能拖垮命令
                self._log_hook_failure("输出", exc)

    def emit_output_text(self, stream: StreamName, data: str, exec_id: str | None = None) -> None:
        """`emit_output` 的便捷包装（provider 回调把 SDK 的分块直接喂进来）。"""
        self.emit_output(SandboxOutputChunk(stream=stream, data=data, exec_id=exec_id))

    # -------------------------------------------------------------------- usage 钩子

    def add_usage_handler(self, handler: UsageHandler) -> None:
        """注册计费分段回调（manager 用它把封口段转成 `sandbox_usage` 行）。"""
        if handler not in self._usage_handlers:
            self._usage_handlers.append(handler)

    def remove_usage_handler(self, handler: UsageHandler) -> None:
        """注销计费分段回调（幂等）。"""
        if handler in self._usage_handlers:
            self._usage_handlers.remove(handler)

    def emit_usage(self, segment: SandboxUsageSegment) -> None:
        """分发一个计费分段（manager 封口段时调用；回调失败只记日志）。"""
        for handler in self._usage_handlers:
            try:
                handler(segment)
            except Exception as exc:  # noqa: BLE001 —— 见 emit_output 同款理由
                self._log_hook_failure("usage", exc)

    # ------------------------------------------------------- round-0 兜底基线（AC-002-5）

    def is_git_repository(self) -> bool:
        """沙箱内工作目录是否已是一个 git 仓库（round-0 分支判据，§4.4 三分支）。"""
        result = self.run_command(GIT_PROBE_COMMAND, timeout=BASELINE_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            return False
        return result.output.strip().splitlines()[-1].strip() == "true"

    def create_init_baseline(self, *, note: str | None = None) -> BaselineInit:
        """非 git 输入（本地文件夹/空目录）的 round-0 基线：init + add + commit + tag。

        提交消息用契约 `BaselineCommitMessage.render()` 渲染（`[lcode-baseline] init: …`），
        tag 用 `tag_for_round(0)`（`lcode-baseline/round-0`）——命名空间隔离由契约保证。

        已在 git 仓库时**拒绝执行**（`SandboxError`）：git 输入分支的语义是"只用输入仓库
        自身历史、不做任何 git 写操作"（AC-002-5），绝不在用户仓库上凭空 init/commit。
        """
        if self.is_git_repository():
            raise SandboxError(
                "工作目录已是 git 仓库：round-0 兜底基线只适用于非 git 输入"
                "（AC-002-5 的 git 输入分支不写 git）"
            )

        message = BaselineCommitMessage(kind="init", round=0, note=note).render()
        tag = tag_for_round(0)

        self._git(["init", "-q"])
        self._git(["add", "-A"])
        # 沙箱内通常没有 git 身份配置：显式 -c 传入，不改沙箱的 git config。
        # --allow-empty：空目录输入（§4.4 分支 3）也必须留下一个基线提交。
        self._git(
            [
                "-c",
                f"user.name={GIT_IDENTITY_NAME}",
                "-c",
                f"user.email={GIT_IDENTITY_EMAIL}",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                message,
            ]
        )
        self._git(["tag", tag])
        output = self._git(["rev-parse", "HEAD"])

        match = COMMIT_SHA_RE.search(output)
        if match is None or not check_commit_sha(match.group(0)):
            raise SandboxError(f"基线提交后无法解析 commit sha：{output.strip()!r}")
        return BaselineInit(commit=match.group(0), tag=tag, message=message)

    def probe_toolchain(
        self, probes: Iterable[tuple[str, str]] = TOOLCHAIN_PROBES
    ) -> dict[str, CommandProbe]:
        """Q7 探针：逐条执行工具链检查命令，返回 {名字: 探针结果}（不抛错）。"""
        results: dict[str, CommandProbe] = {}
        for name, command in probes:
            try:
                result = self.run_command(command, timeout=BASELINE_TIMEOUT_SECONDS)
            except SandboxError:
                results[name] = CommandProbe(command=command, exit_code=None, output="")
                continue
            results[name] = CommandProbe(
                command=command, exit_code=result.exit_code, output=result.output.strip()
            )
        return results

    # -------------------------------------------------------------------- 内部工具

    def _set_sandbox_id(self, sandbox_id: str) -> None:
        """provider provision 成功后登记真实沙箱 id（子类调用）。"""
        self._sandbox_id = sandbox_id

    def _git(self, args: Sequence[str]) -> str:
        """在沙箱内跑一条 git 命令；非零退出即抛 `SandboxError`（带命令与输出）。"""
        command = "git " + " ".join(shlex.quote(arg) for arg in args)
        result = self.run_command(command, timeout=BASELINE_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            raise SandboxError(
                f"沙箱内 git 命令失败（exit={result.exit_code}）："
                f"{command}\n{result.output.strip()}"
            )
        return result.output

    def _log_hook_failure(self, what: str, exc: Exception) -> None:
        """回调链失败只记 warning（业务日志体系唯一出口，ADR-006 铁律 11）。"""
        log = BusinessLog(
            message=f"沙箱{what}回调失败（已忽略，不影响命令结果）",
            action="沙箱回调",
            entity="sandbox",
            entity_id=self.id,
        )
        log.warning(exc)

    def _require_id(self) -> str:
        """取真实沙箱 id；未 provision 即抛（调用方漏 create 的编程错误）。"""
        if self._sandbox_id is None:
            raise SandboxLifecycleError("沙箱尚未 provision（先调用 create()）")
        return self._sandbox_id


def probe_required(probes: Mapping[str, CommandProbe]) -> list[str]:
    """从探针结果里挑出缺失的工具名（Q7 spike 与真沙箱冒烟的共用判据）。"""
    return [name for name, probe in probes.items() if not probe.present]


def _describe_error(exc: BaseException) -> str:
    """未知异常 → 后端特定错误串（deepagents 允许；不泄漏堆栈）。"""
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "BASELINE_TIMEOUT_SECONDS",
    "GIT_PROBE_COMMAND",
    "TOOLCHAIN_PROBES",
    "BaselineInit",
    "CommandProbe",
    "LcodeSandbox",
    "OutputHandler",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxFileError",
    "SandboxLifecycleError",
    "SandboxOutputChunk",
    "SandboxPhase",
    "SandboxProvider",
    "SandboxProvisionNotice",
    "SandboxStatus",
    "SandboxUnavailable",
    "SandboxUsageSegment",
    "StableSandboxPhase",
    "StreamName",
    "UsageHandler",
    "UsagePhase",
    "ProvisionObserver",
    "ProvisionPhase",
    "combine_output",
    "probe_required",
]
