"""unit/requires_llm：真网关用例扩展（M2 收口台账 C4/C5/C6）。

为什么单独成文件（而不是塞进 test_llm_factory.py）：那个文件有 autouse 的
`_fake_gateway` fixture，把 `llm_settings.base_url` 与密钥都打桩成假值——真网关用例
一旦共用该 fixture 就会打向 `llm-gateway.invalid`。本文件不装任何打桩，直接读
`.env`/进程环境；缺 `LLM_BASE_URL` 或 `LLM_API_KEY_ENV` 指向的变量时**显式 skip**
（非失败，test-plan §1.4）。

覆盖：
- C4：真实流式调用的文本 chunk + usage_metadata（input/output tokens > 0），并验证
  usage 可被 runtime 的 `_absorb_message_usage` 口径吸收（运行级 Usage 累计 > 0）；
- C5：thinking 模型开启 reasoning 后返回 thinking 内容且带 signature 字段；各档位
  映射可构造不报错；temperature 按 param_support 被剥离；
- C6：models.yaml 中由 AgentRef（角色路由）引用的模型条目可被 get_llm 解析，并完成
  一次最小 messages 真实调用返回非空文本。

断言纪律：不同网关方言下只锁不变量（非空文本、usage 正数、thinking 块存在），
不锁具体 token 数/文案/思考长度。
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage

from app.agent.llm_factory import get_llm
from app.agent.models_registry import Role, model_registry
from app.agent.runtime import RunExecutor
from app.core.config import llm_settings, safe_env_var_name
from lcode_contracts.common import EffortLevel, Usage

# ---------------------------------------------------------------- 显式豁免标记

_LLM_ENV_READY = bool(llm_settings.base_url) and bool(os.environ.get(llm_settings.api_key_env))

pytestmark = [
    pytest.mark.requires_llm,
    pytest.mark.skipif(
        not _LLM_ENV_READY,
        reason=(
            "真网关用例需要 LLM_BASE_URL（Anthropic Messages 兼容网关）非空且 "
            f"{safe_env_var_name(llm_settings.api_key_env)} 已 export；"
            "缺任一则显式 skip（非失败，test-plan §1.4）"
        ),
    ),
]


# ---------------------------------------------------------------- 工具


class _NullSink:
    """RunExecutor 的占位 sink：本文件只复用 usage 吸收逻辑，不驱动 astream。"""

    async def on_raw_event(self, raw: Any) -> None:  # pragma: no cover - 不被调用
        pass


def _message_text(message: Any) -> str:
    """提取消息文本：兼容 str 内容与 Anthropic 内容块列表（只取 text 块）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _thinking_blocks(message: Any) -> list[dict[str, Any]]:
    """取 Anthropic thinking 块（内容块列表形态；非该方言返回空表）。"""
    content = getattr(message, "content", "")
    if not isinstance(content, list):
        return []
    return [
        block for block in content if isinstance(block, dict) and block.get("type") == "thinking"
    ]


# ---------------------------------------------------------------- C4：流式 + usage


def test_c4_real_stream_emits_text_and_usage_absorbable_by_runtime():
    """C4：真实流式返回文本 chunk 与 usage_metadata，且 runtime 口径能吸收。

    - 文本不锁内容，只锁非空；
    - usage 只锁 input/output > 0（不锁具体 token 数，网关/模型不同会漂移）；
    - `_absorb_message_usage` 是 runtime 的 run 级 usage 唯一吸收口（F3 口径），
      这里按真实 chunk 序列喂一遍，断言折叠后的 Usage 正数——等价于 run 结束
      `_outcome` 的兜底路径。
    """

    llm = get_llm("router")  # 经济档：流式冒烟成本最低
    chunks = list(llm.stream([HumanMessage(content="只回复一个字：好")]))  # 消费到流自然结束
    assert chunks, "流式调用未产出任何 chunk"

    text = "".join(_message_text(chunk) for chunk in chunks)
    assert text.strip(), "流式调用未产出文本内容"

    usage_metas = [
        meta
        for chunk in chunks
        if isinstance((meta := getattr(chunk, "usage_metadata", None)), dict)
    ]
    assert usage_metas, "流式响应未携带 usage_metadata（usage 帧缺失）"

    final = usage_metas[-1]
    assert int(final.get("input_tokens") or 0) > 0, f"usage input_tokens 非正: {final}"
    assert int(final.get("output_tokens") or 0) > 0, f"usage output_tokens 非正: {final}"

    executor = RunExecutor(
        session_id="c4-session",
        run_id="c4-run",
        sink=_NullSink(),
        run_timeout_s=1,
    )
    usage = Usage()
    for chunk in chunks:
        executor._absorb_message_usage(chunk, usage)
    executor._flush_message_usage(usage)  # 方言无 "last" 标记时的残窗兜底
    assert usage.input_tokens > 0, "runtime 口径未吸收到 input_tokens"
    assert usage.output_tokens > 0, "runtime 口径未吸收到 output_tokens"


# ---------------------------------------------------------------- C5：thinking + 档位


def test_c5_thinking_model_returns_thinking_block_with_signature():
    """C5：thinking 模型开 reasoning 后返回 thinking 内容，块内带 signature 字段。

    ADR-007 的核心收益：Anthropic 方言保留源思维链与签名（多轮可无损回传）。
    只锁不变量：存在 thinking 块、思考文本非空、signature 字段存在；不锁长度与取值。
    """

    llm = get_llm("main", effort_override="high")
    assert llm.thinking is not None, "high 档位应启用 thinking"

    message = llm.invoke([HumanMessage(content="用一句话说明为什么 1+1=2")])
    blocks = _thinking_blocks(message)
    assert blocks, "thinking 模型响应中未找到 thinking 内容块"
    assert any(str(block.get("thinking") or "").strip() for block in blocks), "thinking 块内容为空"
    assert all("signature" in block for block in blocks), "thinking 块缺少 signature 字段"


def test_c5_all_declared_efforts_map_without_error_and_strip_temperature():
    """C5：models.yaml 声明的每个档位都能构造；temperature 按 param_support 被剥离。"""

    spec = model_registry.models["qwen3.7-plus"]
    assert spec.supports_reasoning
    for effort in spec.reasoning_efforts:
        llm = get_llm("main", effort_override=effort, temperature=0.7)
        assert llm.temperature is None, f"档位 {effort} 未按口径剥离 temperature"
        if effort == "off":
            assert llm.thinking is None
        else:
            assert llm.thinking is not None
            assert spec.effort_dialect is not None
            assert llm.thinking == dict(spec.effort_dialect[effort])


# ---------------------------------------------------------------- C6：AgentRef → 模型解析


def _agent_model_bindings() -> dict[str, Role]:
    """models.yaml roles（AgentRef 角色路由）→ 模型 id 的反向索引（每模型取首个角色）。"""

    bindings: dict[str, Role] = {}
    for role, role_spec in model_registry.roles.items():
        if role_spec.model is not None:
            bindings.setdefault(role_spec.model, cast("Role", role))
    return bindings


@pytest.mark.parametrize(("model_id", "role"), sorted(_agent_model_bindings().items()))
def test_c6_agent_ref_model_resolves_and_replies(model_id: str, role: Role):
    """C6：被角色路由引用的模型条目可被 get_llm 解析，并真实调用返回非空文本。

    reasoning 模型传 `off` 档（不启用 thinking）以压低冒烟成本；非 reasoning 模型
    保持角色默认（不传档位）。
    """

    spec = model_registry.models[model_id]
    effort_override: EffortLevel | None = "off" if spec.supports_reasoning else None
    llm = get_llm(role, model_override=model_id, effort_override=effort_override)
    assert llm.model == model_id

    reply = llm.invoke([HumanMessage(content="回复一个字：好")])
    assert _message_text(reply).strip(), f"模型 {model_id} 返回空文本"
