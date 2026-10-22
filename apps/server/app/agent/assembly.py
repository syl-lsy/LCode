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
  prompt caching / HumanInTheLoopMiddleware（interrupt_on= 命中时，graph.py:830-835）
```

审批装配（M3，m3-spikes §1.2 实测口径，**不要改回自装 HITL**）：

- `interrupt_on=build_interrupt_on(mode=approval_mode)` 交给 `create_deep_agent`——
  主图 tail 自动装 `HumanInTheLoopMiddleware`；
- 声明式子智能体**自动继承**同一 `interrupt_on`（`graph.py:686`
  `spec.get("interrupt_on", interrupt_on)`），无需逐个子智能体手挂；
- 子图共享父 checkpointer（`create_sub_agent` 编译不传 saver），resume 只需在父 thread
  发顶层 `Command(resume=...)`。因此本函数**不接 checkpointer**（父图已挂）。

不传的东西（红线）：
- `permissions=`：沙箱 backend 下被安装版拒收（filesystem.py:875）；
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
from langchain.agents.middleware import InterruptOnConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from app.agent.auto_approval import AutoApprovalRuntime, build_auto_approval_middleware
from app.agent.llm_factory import get_llm
from app.agent.middleware import LCodeEventTapMiddleware, build_tool_retry_middleware
from app.agent.models_registry import SessionModelCfg
from app.agent.permissions import ApprovalMode, build_interrupt_on
from app.agent.state import SessionCfg
from app.agent.subagents import build_subagents, load_prompt

ModelFactory = Callable[[str], BaseChatModel]
"""按角色取模型的工厂形态（默认 get_llm 的 session_cfg 预绑定；测试注入假模型）。"""


def default_model_factory(
    session_cfg: SessionModelCfg | SessionCfg | dict[str, str | None] | None,
) -> ModelFactory:
    """get_llm + 会话级两旋钮的预绑定（优先级链求值在 models_registry.resolve）。

    **入参形态必须在这里归一化**：`RunExecutor.run` 直接把它收到的 state 快照（dict，
    `SessionCfg`）交给默认工厂，而 `models_registry.resolve` 只认 `SessionModelCfg`
    （按属性取值）。旧实现把 dict 原样 partial 进去，真实模型路径在 `resolve` 处
    `'dict' object has no attribute 'model'` 崩溃——假模型车道覆盖了工厂接缝所以
    长期没暴露（M3 Phase 6 真机取证发现）。归一化放本函数：所有调用方（runtime 默认
    工厂、显式构造）都得到同一语义，`None` / DTO 形态零改动。
    """
    return partial(get_llm, session_cfg=_session_cfg_from(session_cfg))


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
    approval_mode: ApprovalMode | str = "manual",
    auto_approval: AutoApprovalRuntime | None = None,
) -> CompiledStateGraph:
    """装配内层 deepagents 实例（middleware 栈唯一装配点；每次 run 重建，AC-014-2）。

    `approval_mode` 来自 run 启动快照（sessions.approval_mode，服务层注入；见
    `message_service.RunLaunch`）——resume 段固定用同一快照值，避免决策窗口内
    会话模式被改导致 when 翻转（m3-plan D5 / Phase 1 F）。

    AUTO 模式额外装配 `AutoApprovalMiddleware`（REQ-013 AC-013-3）：分类器在
    model-call 后置钩子里决策、写审计行、把决策写进 state 供 HITL `when` 读取。
    MANUAL 模式**不装配**该 middleware——分类器零调用、零审计行（行为与 Phase 1
    逐字一致）。`auto_approval=None` 时按装配缺陷处理：任何 auto_approve 决策都会
    降级人工审批（fail-closed，见 auto_approval 模块注释）。
    """
    resolved_cfg = _session_cfg_from(session_cfg)
    factory = model_factory if model_factory is not None else default_model_factory(resolved_cfg)

    user_middleware = [
        build_tool_retry_middleware(),
        LCodeEventTapMiddleware(),
    ]
    if approval_mode == "auto":
        # 必须在 build_interrupt_on 之前不做任何判定：mode 合法性由后者 fail loudly。
        user_middleware.append(build_auto_approval_middleware(auto_approval))

    # M3 审批闸门：tail 自动装 HITL；子智能体继承同一策略（m3-spikes §1.2）。
    # 窄类型 dict[str, InterruptOnConfig] 与 create_deep_agent 的 bool|config 联合不协变
    # （dict 值不协变）——在这里放宽到声明形态，不改变运行期内容。
    interrupt_on: dict[str, bool | InterruptOnConfig] = dict(build_interrupt_on(mode=approval_mode))

    return create_deep_agent(
        model=factory("main"),
        tools=tools or [],
        system_prompt=load_prompt("main"),
        middleware=user_middleware,
        subagents=build_subagents(factory),
        backend=backend,
        interrupt_on=interrupt_on,
    )


__all__ = ["ModelFactory", "build_inner_agent", "default_model_factory"]
