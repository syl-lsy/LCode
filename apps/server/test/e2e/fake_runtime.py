"""e2e 进程内替身装配（T7；test-plan-m2 §3.1/§3.2）：假模型剧本路由 + FakeSandbox 注入。

只在 e2e 服务子进程内运行（`server_entry.py` 按 `LCODE_E2E_*` 开关调用 install_*）；
unit/integration 不经本文件（各自直接用 `test/support`）。替身与三层共用的实现一致：

- 假模型 = `ScriptedChatModel`（剧本来自 `test/support/scripts.py`，含 usage 元数据）；
  `E2ERoutedModel` 只做**路由**：按本轮最后一条用户消息的内容关键词选剧本——单轮剧本
  （长文/执行命令/失败/长输出/成本/默认）每次 run 建新实例（互不串步），多轮剧本
  （MULTI_TURN）跨 run 复用同一实例（第二步等第二轮消费，AC-001-3 的剧本语义）。
- 假沙箱 = `FakeSandbox`（本地 tmp + 真子进程），每会话一个工作目录；默认剧本按命令
  文本标记匹配（`ExecStep.match`）：`lcode-stream-line` → 两块延时 stdout（AC-002-1
  实时链），`lcode-fail-sandbox` → 抛 `SandboxUnavailable`（AC-004-4 失败如实呈现）；
  其余命令照常真跑本地子进程（文件工具派生命令都是真链路）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from langchain_core.language_models.base import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from app.sandbox.base import SandboxUnavailable
from test.support import scripts
from test.support.fake_llm import ScriptedChatModel
from test.support.fake_sandbox import ExecStep, FakeSandbox

# --------------------------------------------------------------------- 剧本路由表

RouteBuilder = Callable[[], list[AIMessage]]

# (关键词, 剧本构造器, ScriptedChatModel 额外参数)；匹配 = 关键词是用户消息的子串。
# 长文剧本 chunk_size=7：每个 token（"tokNNN "）恰好一块 → 600 块（AC-003-1 的 ≥500）。
ROUTES: tuple[tuple[str, RouteBuilder, dict[str, Any]], ...] = (
    ("长文", scripts.long_500_tokens, {"chunk_size": 7}),
    ("执行命令", scripts.tool_execute, {}),
    ("失败的命令", scripts.tool_fail, {}),
    ("长输出", scripts.stop_stream, {"chunk_size": 8, "delay_seconds": 0.05}),
    ("第一轮", scripts.multi_turn, {}),
    ("刚才", scripts.multi_turn, {}),
    ("成本", scripts.simple_reply, {}),
)
DEFAULT_ROUTE: tuple[str, RouteBuilder, dict[str, Any]] = ("default", scripts.simple_reply, {})

PERSISTENT_SCENARIOS = frozenset({"multi_turn"})
"""跨 run 复用同一模型实例的剧本（构造器函数名；steps 顺序 = 轮次）——多轮剧本的两条
触发词（第一轮/刚才）必须落到**同一个**实例（第二步等第二轮消费）；其余剧本每次 run
新实例（场景/会话互不串步）。"""

DEFAULT_EXEC_SCRIPT: tuple[ExecStep, ...] = (
    ExecStep(
        stdout_chunks=scripts.EXEC_STREAM_LINES, delay_seconds=0.05, match="lcode-stream-line"
    ),
    ExecStep(
        raises=SandboxUnavailable("e2e 剧本设定：命令命中不可用剧本"), match="lcode-fail-sandbox"
    ),
)
"""每会话假沙箱的默认剧本：按命令标记分流（不命中的命令真跑本地子进程）。"""


class E2ERoutedModel(BaseChatModel):
    """按用户消息关键词路由到场景剧本的进程级共享假模型（`ScriptedChatModel` 的路由壳）。

    run 内首次模型调用的判定：prompt 的最后一条消息是 HumanMessage（后续调用的最后一条
    是 ToolMessage/历史）——此时按路由选/建剧本模型；同 run 后续调用复用它（工具循环的
    多次模型调用消费同一剧本的后续步）。
    """

    _persistent: dict[str, ScriptedChatModel] = PrivateAttr(default_factory=dict)
    _current: ScriptedChatModel | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "lcode-e2e-router"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],  # noqa: ARG002
        *,
        tool_choice: str | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """create_deep_agent 会调它绑工具面；路由壳直接返回自身（同 ScriptedChatModel）。"""
        return self

    def _route(self, content: str) -> tuple[str, RouteBuilder, dict[str, Any]]:
        for keyword, builder, kwargs in ROUTES:
            if keyword in content:
                return keyword, builder, kwargs
        return DEFAULT_ROUTE

    def _scenario_for(self, messages: list[BaseMessage]) -> ScriptedChatModel:
        if self._current is not None and not isinstance(messages[-1], HumanMessage):
            return self._current  # 同一 run 的后续模型调用：复用本轮剧本模型
        content = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                content = str(message.content)
                break
        name, builder, kwargs = self._route(content)
        scenario_key = builder.__name__  # 剧本函数名 = 场景身份（同剧本的不同触发词共享）
        if scenario_key in PERSISTENT_SCENARIOS:
            model = self._persistent.get(scenario_key)
            if model is None:
                model = ScriptedChatModel(steps=builder(), role=name, **kwargs)
                self._persistent[scenario_key] = model
        else:  # 一次性剧本：每次 run 新实例（场景互不串步，会话间互不串步）
            model = ScriptedChatModel(steps=builder(), role=name, **kwargs)
        self._current = model
        return model

    # ------------------------------------------------------------------ 委托面

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        return self._scenario_for(messages)._generate(messages, stop, run_manager, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Any = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ):
        scenario = self._scenario_for(messages)
        async for chunk in scenario._astream(messages, stop, run_manager, **kwargs):
            yield chunk


# --------------------------------------------------------------------- 注入面


def install_fake_llm(app: Any) -> None:
    """把剧本路由假模型注入 model_factory 依赖接缝（e2e 子进程专用）。"""
    from app import dependencies

    router = E2ERoutedModel()

    def _factory_override() -> Callable[[str], BaseChatModel]:
        return lambda role: router  # noqa: ARG005 —— 角色 irrelevant（剧本即行为）

    app.dependency_overrides[dependencies.get_model_factory] = _factory_override


def install_fake_sandbox() -> None:
    """把 FakeSandbox 工厂注入进程级沙箱管理器（事件桥/计费分段随 setter 自动跟随）。"""
    from app import dependencies
    from app.sandbox.base import SandboxProvider
    from app.sandbox.manager import SandboxManager

    root = Path(os.environ.get("LCODE_E2E_SANDBOX_ROOT", "tmp/e2e/sandboxes"))
    root.mkdir(parents=True, exist_ok=True)

    def factory(provider: str, session_id: str) -> FakeSandbox:
        return FakeSandbox(
            root=root / session_id,
            session_id=session_id,
            provider=cast("SandboxProvider", provider),
            sandbox_id=f"fake-e2e-{session_id[:8]}",
            exec_script=list(DEFAULT_EXEC_SCRIPT),
        )

    bridges = dependencies.build_sandbox_event_bridges(dependencies.get_event_pipeline())
    dependencies.set_sandbox_manager(
        SandboxManager(
            factory=factory,
            on_lifecycle=bridges[0],
            on_output=bridges[1],
            on_usage_segment=bridges[2],
        )
    )


__all__ = [
    "DEFAULT_EXEC_SCRIPT",
    "DEFAULT_ROUTE",
    "E2ERoutedModel",
    "PERSISTENT_SCENARIOS",
    "ROUTES",
    "install_fake_llm",
    "install_fake_sandbox",
]
