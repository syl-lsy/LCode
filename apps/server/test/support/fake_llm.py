"""ScriptedChatModel：剧本化假模型（test-plan-m2 §3.2；T4 车道基建）。

设计依据（对安装版 langchain-core 1.6.12 / langgraph 1.2.16 的实测结论，本车道
探针已验证）：

- `create_agent`/`create_deep_agent` 会对模型调 `bind_tools(tools)` —— 假模型覆写为
  返回自身（工具由剧本决定是否调用，不做真绑定）；
- `model.ainvoke` 在 `_astream` 存在时走流式路径，`_astream` 必须产出
  **`ChatGenerationChunk`**（包 `AIMessageChunk`）——直接 yield 裸 chunk 会在
  `_agenerate_with_cache` 的 `generation_info` 处炸掉（实测 AttributeError）；
- messages 流模式把 chunk 的 `usage_metadata` / `tool_calls` / `id` 原样带出
  （AC-003-1 / AC-015-1 的唯一数据源），故剧本步在 chunk 上回放这三个字段；
- 剧本耗尽后**重复最后一步**（多轮/多 invoke 的可预测行为）并照常记录 prompts。

可观测面（测试断言用）：
- `prompts`：每次模型调用收到的完整消息列表（AC-001-3 / AC-004-2 多轮上下文断言）；
- `invocations`：调用次数（usage 测试断言"每 invocation 一次"）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.base import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import Field, PrivateAttr


def usage_of(input_tokens: int, output_tokens: int) -> dict[str, int]:
    """造一个 usage_metadata（langchain 标准三键形态）。"""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


class ScriptedChatModel(BaseChatModel):
    """按剧本逐步回复的假模型：`steps` 顺序消费，支持文本分块与块间延时。"""

    steps: Sequence[BaseMessage] = Field(default_factory=list)
    """剧本：每步一条 AIMessage（可带 tool_calls / usage_metadata）。"""

    chunk_size: int = 0
    """>0 时把该步 content 按 N 字符分块流式产出（0 = 整条一块）。"""

    delay_seconds: float = 0.0
    """每个流式块之间的延时（stop 测试用，给取消留窗口）。"""

    usage_every_chunk: bool = False
    """True = 每个流式块都带该步的 usage（累计量方言；translator 的 `_absorb_usage` 明列支持）。

    默认 False（仅末块给总量，贴近 Anthropic/OpenAI 主流网关）。两种方言都是
    「同一消息内分项取最大」的合法输入；差别只在**并发交错流式**时暴露：累计方言下，
    任何「按消息 id 折叠的单窗口」消费者会把同一条消息的累计量随窗口切换重复折叠。
    `test_usage_reconciliation.py` 用它锁定「残差基准必须与逐次记账同源」。
    """

    role: str = "fake"
    """角色标注（测试断言 model_factory 按角色取模型用，不参与行为）。"""

    idx: int = 0
    """下一个要消费的剧本步下标（耗尽后停在最后一步重复）。"""

    prompts: list[list[BaseMessage]] = Field(default_factory=list)
    """每次模型调用收到的消息列表（追加；多轮上下文断言的锚）。"""

    _invocations: int = PrivateAttr(default=0)

    @property
    def invocations(self) -> int:
        return self._invocations

    @property
    def _llm_type(self) -> str:
        return "lcode-scripted"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],  # noqa: ARG002 —— 假模型不真绑定
        *,
        tool_choice: str | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """create_agent 会调它绑工具面；假模型直接返回自身，剧本决定调用。

        签名镜像 `BaseChatModel.bind_tools`（mypy override 检查要求）。
        """
        return self

    # ------------------------------------------------------------------ 内部

    def _next_step(self, messages: list[BaseMessage]) -> AIMessage:
        self.prompts.append(list(messages))
        self._invocations += 1
        index = min(self.idx, len(self.steps) - 1)
        self.idx += 1
        step = self.steps[index]
        if not isinstance(step, AIMessage):
            raise TypeError(f"剧本步必须是 AIMessage，得到 {type(step).__name__}")
        return step

    @staticmethod
    def _split(text: str, size: int) -> list[str]:
        if size <= 0 or not text:
            return [text]
        return [text[i : i + size] for i in range(0, len(text), size)]

    # ------------------------------------------------------------------ 同步/异步面

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        step = self._next_step(messages)
        return ChatResult(generations=[ChatGeneration(message=step)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Any = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,
    ):
        step = self._next_step(messages)
        pieces = self._split(str(step.content), self.chunk_size)
        tool_calls = list(getattr(step, "tool_calls", []) or [])
        usage = step.usage_metadata
        for order, piece in enumerate(pieces):
            if self.delay_seconds and order > 0:
                await asyncio.sleep(self.delay_seconds)
            carry_usage = self.usage_every_chunk or order == len(pieces) - 1
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    id=step.id,
                    # tool_calls 只挂首块；usage 默认只挂末块（累计方言下每块都挂）
                    tool_calls=tool_calls if order == 0 else [],
                    usage_metadata=usage if carry_usage else None,
                )
            )


__all__ = ["ScriptedChatModel", "usage_of"]
