"""工具调用的 exec_id 传播（跨车道接缝：tap 中间件 → 沙箱适配器）。

契约背景（`lcode_contracts/events.py::SandboxOutputEvent`）：stdout/stderr 通道必须带
`exec_id`（= 发起命令的 tool_call_id，终端据此标注输出来源，AC-002-1）。安装版
deepagents 0.6.12 的 execute 工具调 `backend.execute(command, timeout=...)` **不传**
exec_id（该参数是 LCode 扩展，本仓 deepagents 全源无此符号，grep 实证）——注解由两端
接力完成：

- **发起端**：`LCodeEventTapMiddleware.awrap_tool_call` 在 handler 执行前把 tool_call_id
  写入本 ContextVar（asyncio 任务上下文隔离，并行工具调用互不串线）；
- **接收端**：`LcodeSandbox.run_command` 的 `exec_id=None` 时回退读本变量（显式参数
  优先；tap 上下文之外的调用——manager 基线助手、无工具路径——读到 None，输出块由
  上游桥接按契约诚实跳过）。

依赖方向（A3 §1.2）：`utils/` 是叶子，`agent/` 与 `sandbox/` 都可安全引用本模块，
不引入 agent→sandbox 或反向的新依赖边。
"""

from __future__ import annotations

from contextvars import ContextVar

EXEC_ID_CONTEXT: ContextVar[str | None] = ContextVar("lcode_exec_id", default=None)
"""当前工具调用的 exec_id（None = 无工具上下文；值由 tap 中间件写入）。"""


__all__ = ["EXEC_ID_CONTEXT"]
