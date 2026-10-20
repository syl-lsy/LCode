"""沙箱工厂：provider → 适配器实例（AC-002-6；A3 §4.2 末段）。

**新增 provider = 新增一个 `LcodeSandbox` 子类 + 在 `PROVIDERS` 注册一项**——上层
（deepagents backend 注入、`sandbox_service`、`manager`）零改动，这是 AC-002-6
"provider 可切换"的结构性保证。

provider 的取值来源有两条，优先级固定：

1. 调用方显式传入（会话创建时用 `sessions.sandbox_provider` 列的值——**会话级固化**，
   改全局配置只影响新会话）；
2. 未传时读 `sandbox_settings.provider`（当前全局配置）。

工厂只**构造**适配器对象（不产生任何网络调用/资源），provision 发生在
`LcodeSandbox.create()`——因此"先定级再开沙箱"的成本可控，未知 provider 在构造期
即报 `SandboxConfigurationError`，不会留下半成品沙箱。
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import SandboxSettings, sandbox_settings
from app.sandbox.bailian import BailianSandbox
from app.sandbox.base import LcodeSandbox, SandboxConfigurationError
from app.sandbox.opensandbox import OpenSandboxSandbox

SandboxBuilder = Callable[..., LcodeSandbox]
"""适配器构造器签名：`(keyword args) -> LcodeSandbox`（统一收 `session_id` / `settings`）。"""

PROVIDERS: dict[str, SandboxBuilder] = {
    "bailian": BailianSandbox,  # E2B 协议（e2b==2.31.0 钉死，硬约束 1）
    "opensandbox": OpenSandboxSandbox,  # 自有协议（opensandbox==1.1.1 锁版本线）
}


def create_sandbox_client(
    provider: str | None = None,
    session_id: str | None = None,
    *,
    settings: SandboxSettings | None = None,
) -> LcodeSandbox:
    """按 provider 造一个未 provision 的沙箱客户端。

    签名与 `manager.SandboxFactory` 兼容（`(provider, session_id)` 位置传参），
    因此本函数可直接作为 manager 的工厂注入。

    Args:
        provider: `bailian` | `opensandbox`；None = 用当前全局配置
            （会话级固化由调用方传入 `sessions.sandbox_provider` 的值实现）。
        session_id: 归属会话（只作日志/占位 id 用；沙箱本身与会话一一对应由 manager 管）。
        settings: 配置快照（默认取全局单例；测试可注入）。

    Raises:
        SandboxConfigurationError: provider 未注册（消息列出已注册 provider）。
    """
    cfg = settings or sandbox_settings
    resolved = provider or cfg.provider
    builder = PROVIDERS.get(resolved)
    if builder is None:
        raise SandboxConfigurationError(
            f"未知沙箱 provider：{resolved!r}（已注册：{sorted(PROVIDERS)}；"
            "新增 provider = 新增一个 LcodeSandbox 子类 + 在 factory.PROVIDERS 注册一项）"
        )
    return builder(session_id=session_id, settings=cfg)


__all__ = ["PROVIDERS", "SandboxBuilder", "create_sandbox_client"]
