"""内层 deepagents 装配（A3 §5.2）：`build_inner_agent` 是 middleware 栈唯一装配点。

装配序（安装版 deepagents 0.6.12 `graph.py` 实测语义）：

```
默认栈（deepagents 生成，勿动）
  TodoListMiddleware                       # 栈首已内置（graph.py:774）——不重复装配（硬约束 2）
  FilesystemMiddleware(backend=sandbox)    # backend = 会话沙箱（LcodeSandbox）
  SubAgentMiddleware(subagents=四预置)      # task() 承载；规格编译是急切的（装配期）
  SummarizationMiddleware / PatchToolCallsMiddleware
用户 middleware（graph.py:811-812：默认栈后、tail 前追加）
  ToolRetryMiddleware（幂等白名单）
  LCodeEventTapMiddleware
tail（deepagents 生成）
  prompt caching / （M3：HITL——经 interrupt_on= 通路，见 permissions.py）
```

不传的东西（M2 红线）：
- `permissions=`：沙箱 backend 下被安装版拒收（filesystem.py:875）；
- `interrupt_on=`：会自动装配 HITL（graph.py:830-835）——M3 审批车道接线；
- 显式 `TodoListMiddleware`：默认栈已含。

模型来源：`model_factory(role)`（默认 `get_llm(role, session_cfg=session_cfg)`，
优先级链 A3 §3.1）。测试以 `ScriptedChatModel` 工厂注入（A3 §9.2 替身边界），
AC-014-2 热切换 = RunExecutor 每次 run 重读会话快照并重建 agent（每次 run 调一次本函数）。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from deepagents import create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.agent.llm_factory import get_llm
from app.agent.middleware import LCodeEventTapMiddleware, build_tool_retry_middleware
from app.agent.models_registry import SessionModelCfg
from app.agent.state import SessionCfg
from app.agent.subagents import build_subagents, load_prompt

ModelFactory = Callable[[str], BaseChatModel]
"""按角色取模型的工厂形态（默认 get_llm 的 session_cfg 预绑定；测试注入假模型）。"""


def default_model_factory(session_cfg: SessionModelCfg | None) -> ModelFactory:
    """get_llm + 会话级两旋钮的预绑定（优先级链求值在 models_registry.resolve）。"""
    return partial(get_llm, session_cfg=session_cfg)


def _session_cfg_from(
    session_cfg: SessionModelCfg | SessionCfg | dict[str, str | None] | None,
) -> SessionModelCfg | None:
    """state 的 SessionCfg 快照（dict）→ models_registry 的 SessionModelCfg（DTO）。"""
    if session_cfg is None or isinstance(session_cfg, SessionModelCfg):
        return session_cfg
    return SessionModelCfg(
        model=session_cfg.get("model"),
        reasoning_effort=session_cfg.get("reasoning_effort"),  # type: ignore[arg-type]
    )


def build_inner_agent(
    session_cfg: SessionModelCfg | SessionCfg | dict[str, str | None] | None,
    backend: BackendProtocol,
    *,
    model_factory: ModelFactory | None = None,
    tools: list[BaseTool] | None = None,
    checkpointer: Checkpointer | None = None,
) -> CompiledStateGraph:
    """装配内层 deepagents 实例（middleware 栈唯一装配点；每次 run 重建，AC-014-2）。"""
    resolved_cfg = _session_cfg_from(session_cfg)
    factory = model_factory if model_factory is not None else default_model_factory(resolved_cfg)

    user_middleware = [
        build_tool_retry_middleware(),
        LCodeEventTapMiddleware(),
    ]

    return create_deep_agent(
        model=factory("main"),
        tools=tools or [],
        system_prompt=load_prompt("main"),
        middleware=user_middleware,
        subagents=build_subagents(factory),
        backend=backend,
        # M2 红线：不传 permissions（沙箱 backend 拒收）/ interrupt_on（HITL 是 M3）
        checkpointer=checkpointer,
    )


__all__ = ["ModelFactory", "build_inner_agent", "default_model_factory"]
