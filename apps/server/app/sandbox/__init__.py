"""沙箱双适配器（ADR-004 偏差 6；A3 §4）：对齐 deepagents `BaseSandbox` 方法面。

模块地图（A3 §4.1 的分层图，逐文件）：

- `base.py`：`LcodeSandbox(BaseSandbox)`——协议 4 抽象成员（execute/upload_files/
  download_files/id）的具体实现 + LCode 扩展（生命周期 create/pause/resume/kill/status、
  输出流回调、usage 分段钩子、round-0 兜底基线、Q7 工具链探针）；
- `bailian.py`：`BailianSandbox`——e2b==2.31.0（**钉死**：更高版本创建百炼沙箱返回
  405，硬约束 1）E2B 协议；
- `opensandbox.py`：`OpenSandboxSandbox`——opensandbox==1.1.1 自有协议（**非 E2B
  兼容**，硬约束 12；升级必须过本包回归）；
- `factory.py`：`create_sandbox_client(provider)`——provider 开关；新增 provider =
  新增一个实现类 + `PROVIDERS` 注册一项（AC-002-6 的结构性保证）；
- `manager.py`：`SandboxManager`——session→sandbox 注册表 / 相位机 / 空闲 pause 计时 /
  意外不可用→crashed+重建 / 计费分段 / 关停策略（A3 §4.3）。

对齐策略（A3 §4.1）：适配器**直接继承** deepagents `BaseSandbox`，文件工具语义（ls/
read/write/edit/grep/glob）由基类经命令执行派生——不另立平行抽象。生命周期
（create/pause/resume/kill）是协议外扩展，落在 `LcodeSandbox` 中间层，不污染对齐面。

依赖禁令（A3 §1.2）：`sandbox/` 不得 import `agent/`、`services/`、`events/`、`models/`
（适配器是薄翻译层；事件发射与落库由上层 service 消费本包的回调/记录完成）。
"""

from app.sandbox.bailian import BailianSandbox
from app.sandbox.base import (
    BaselineInit,
    CommandProbe,
    LcodeSandbox,
    SandboxConfigurationError,
    SandboxError,
    SandboxFileError,
    SandboxLifecycleError,
    SandboxOutputChunk,
    SandboxPhase,
    SandboxProvider,
    SandboxStatus,
    SandboxUnavailable,
    SandboxUsageSegment,
    StableSandboxPhase,
    StreamName,
    UsagePhase,
    combine_output,
    probe_required,
)
from app.sandbox.factory import PROVIDERS, create_sandbox_client
from app.sandbox.manager import (
    LifecycleSink,
    OutputSink,
    RestoreCallback,
    SandboxFactory,
    SandboxLifecycleEvent,
    SandboxManager,
    SandboxRebuildResult,
    SandboxUsageRecord,
    UsageSink,
)

__all__ = [
    "BailianSandbox",
    "BaselineInit",
    "CommandProbe",
    "LifecycleSink",
    "LcodeSandbox",
    "OutputSink",
    "PROVIDERS",
    "RestoreCallback",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxFactory",
    "SandboxFileError",
    "SandboxLifecycleError",
    "SandboxLifecycleEvent",
    "SandboxManager",
    "SandboxOutputChunk",
    "SandboxPhase",
    "SandboxProvider",
    "SandboxRebuildResult",
    "SandboxStatus",
    "SandboxUnavailable",
    "SandboxUsageRecord",
    "SandboxUsageSegment",
    "StableSandboxPhase",
    "StreamName",
    "UsagePhase",
    "UsageSink",
    "combine_output",
    "create_sandbox_client",
    "probe_required",
]
