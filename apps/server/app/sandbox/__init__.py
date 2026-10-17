"""沙箱双适配器（ADR-004 偏差 6；A3 §4）：对齐 deepagents `BaseSandbox` 方法面。

M1 骨架只有包壳，M2 任务 4 按 A3 §4 填肉（文件级计划）：

- `base.py`：`LcodeSandbox(BaseSandbox)`——生命周期骨架 + 输出流回调 + 用量钩子；
- `bailian.py`：`BailianSandbox`（e2b==2.31.0 钉死，E2B 协议）；
- `opensandbox.py`：`OpenSandboxSandbox`（自有协议，锁 1.1.x 版本线）；
- `factory.py`：`create_sandbox_client(provider)`——provider 开关，新增 provider =
  新增一个实现类 + factory 注册一项（AC-002-6）；
- `manager.py`：`SandboxManager`——session→sandbox 注册表 / 空闲 pause 计时 /
  重建（AC-002-4）/ 关停策略。

对齐策略（A3 §4.1）：适配器**直接继承** deepagents `BaseSandbox`，只需实现
`execute / upload_files / download_files / id` 四个抽象成员，文件工具语义由基类
经 `execute()` 派生——不另立平行抽象。生命周期（create/pause/resume/kill）是协议外
扩展，落在 `LcodeSandbox` 中间层，不污染对齐面。

依赖禁令：`sandbox/` 不得 import `agent/` 或 `services/`（A3 §1.2）。
"""
