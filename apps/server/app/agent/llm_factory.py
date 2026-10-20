"""get_llm 模型工厂：模型实例构造的唯一入口（AGENTS.md 接缝表；A3 §3；ADR-007）。

职责（A3 §3.1/§3.3）：
- 优先级链落到 `ChatAnthropic` 实例：请求级 > 会话级 > 角色级 > gateway.default_model
  （链的求值在 ModelRegistry.resolve，这里只消费结果）；
- **max_tokens**：Anthropic Messages 必需参数，恒取 `spec.max_output_tokens`
  （缺省会落 SDK 默认 1024 静默截断输出——ADR-007）；
- **thinking 方言**（AC-014-3 + ADR-007）：`supports_reasoning=false` 或档位未被模型
  声明 → 剥离 no-op + warning（前端置灰的等价后端行为）；`"off"` 或空表 → 不传
  `thinking`（网关自适应）；开档 → `thinking={"type": "enabled", "budget_tokens": N}`。
  方言表由 models.yaml 显式给出（U1 provisional），工厂不做任何硬编码翻译——
  test_effort_dialect_translation 锁"工厂输出 ≡ yaml 表"；
- **防御 clamp**：`budget_tokens >= max_tokens` 时钳到 `max(1024, max_tokens - 1024)`
  + warning（注册表层已拦越界配置，这里是纵深防御，正常配置不可达；下限兜底防小
  max_tokens 产出低于 Anthropic 扩展思考预算下限的预算）；
- **thinking 启用时剥离 temperature/top_k + warning**：Anthropic 扩展思考要求温度语义
  固定，采样参数与 thinking 不兼容；
- **非 thinking 时**：`temperature` 按 `param_support.temperature`（false 则剥离 +
  warning，原硬约束 4 语义）；`top_k` 走 Anthropic 顶层标准参数 `native`（原硬约束 3
  的 extra_body 口径已随 ChatOpenAI 移除）。

密钥口径（A3 §2.2 / NFR-004）：`base_url` 与 `api_key` 在**调用期**读取（import 期
不炸，M1 骨架无 LLM 也可冒烟）；缺失报 RuntimeError 且错误信息只含环境变量名，
永不出现密钥本体。`LLM_BASE_URL` 必须是 **Anthropic Messages 兼容网关**
（`/v1/messages` 端点）——OpenAI-only 网关不再支持（ADR-007）。
"""

from __future__ import annotations

import os
from typing import Any

from langchain_anthropic import ChatAnthropic
from loguru import logger

from app.agent.models_registry import (
    MIN_THINKING_BUDGET,
    Role,
    SessionModelCfg,
    model_registry,
)
from app.core.config import llm_settings
from lcode_contracts.common import EffortLevel

_THINKING_BUDGET_MARGIN = 1024
"""防御 clamp 的预算余量：budget >= max_tokens 时钳到 max_tokens - 本值（ADR-007）。"""


def get_llm(
    role: Role,
    *,
    model_override: str | None = None,
    effort_override: EffortLevel | None = None,
    session_cfg: SessionModelCfg | None = None,
    temperature: float | None = None,
    top_k: int | None = None,
) -> ChatAnthropic:
    """按角色与覆盖链构造 ChatAnthropic（Anthropic Messages 兼容网关，ADR-007）。

    - `model_override`/`effort_override`：请求级参数位（P0 无 UI 来源，为 API/内部
      调用与 P1 fallback 链预留——预留参数不算提前实现）；
    - `session_cfg`：会话级两旋钮（sessions.model / sessions.reasoning_effort）；
    - `temperature`/`top_k`：内部调用旋钮，受 param_support 与 thinking 状态约束
      （见模块 docstring）。
    """

    resolved = model_registry.resolve(
        role,
        model_override=model_override,
        effort_override=effort_override,
        session_cfg=session_cfg,
    )
    spec = resolved.spec

    # 网关配置调用期 fail-fast（错误信息只含变量名，不泄漏密钥本体——NFR-004）
    base_url = llm_settings.base_url
    if not base_url:
        raise RuntimeError(
            "LLM_BASE_URL 未配置：请在 .env 设置 Anthropic Messages 兼容网关地址"
            "（LLM_BASE_URL=...，/v1/messages 端点）后重试"
        )
    key_env = llm_settings.api_key_env
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(
            f"网关密钥缺失：环境变量 {key_env} 未设置"
            f"（LLM_API_KEY_ENV 引用其名字，密钥本体只进环境变量不入库）"
        )

    kwargs: dict[str, Any] = {
        "model": spec.id,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": llm_settings.request_timeout_s,
        "max_retries": llm_settings.max_retries,
        "max_tokens": spec.max_output_tokens,
    }

    # AC-014-3：effort 翻译或置灰剥离（no-op + warning）
    thinking: dict[str, Any] | None = None
    if resolved.effort is not None:
        if not spec.supports_reasoning:
            logger.warning(
                "llm_factory: 剥离 reasoning_effort={!r}（模型 {} 不支持 reasoning，"
                "档位置灰的等价后端行为）",
                resolved.effort,
                spec.id,
            )
        elif resolved.effort not in spec.reasoning_efforts:
            logger.warning(
                "llm_factory: 剥离 reasoning_effort={!r}（模型 {} 未声明支持该档位，支持：{}）",
                resolved.effort,
                spec.id,
                list(spec.reasoning_efforts),
            )
        elif spec.effort_dialect is not None:
            entry = spec.effort_dialect[resolved.effort]
            if entry:  # "off"/空表 → 不传 thinking（网关自适应语义）
                thinking = dict(entry)
                budget = thinking.get("budget_tokens")
                if isinstance(budget, int) and budget >= spec.max_output_tokens:
                    # max(下限, max_tokens - 余量)：小 max_tokens 时不得跌破 Anthropic
                    # 扩展思考预算下限（MIN_THINKING_BUDGET，评审 P2 修复）
                    clamped = max(
                        MIN_THINKING_BUDGET, spec.max_output_tokens - _THINKING_BUDGET_MARGIN
                    )
                    logger.warning(
                        "llm_factory: thinking budget clamp（模型 {} 档位 {} 的"
                        " budget_tokens={} >= max_tokens={}，钳到 {}——"
                        "注册表应已拦此配置，本条为纵深防御痕迹）",
                        spec.id,
                        resolved.effort,
                        budget,
                        spec.max_output_tokens,
                        clamped,
                    )
                    thinking["budget_tokens"] = clamped

    if thinking is not None:
        kwargs["thinking"] = thinking
        # Anthropic 扩展思考要求温度语义固定：thinking 启用时采样参数一律剥离
        if temperature is not None:
            logger.warning(
                "llm_factory: 剥离 temperature（模型 {} thinking 启用，"
                "扩展思考要求温度语义固定，采样参数不兼容——ADR-007）",
                spec.id,
            )
        if top_k is not None:
            logger.warning(
                "llm_factory: 剥离 top_k（模型 {} thinking 启用，采样参数与扩展思考不兼容）",
                spec.id,
            )
    else:
        # 原硬约束 4：param_support.temperature=false → 剥离 + 告警（不向用户暴露）
        if temperature is not None:
            if spec.param_support.temperature:
                kwargs["temperature"] = temperature
            else:
                logger.warning(
                    "llm_factory: 剥离 temperature 参数（模型 {} 声明不支持："
                    "thinking 模型忽略该参数）",
                    spec.id,
                )
        # ADR-007：top_k 是 Anthropic Messages 顶层标准参数，直接透传
        if top_k is not None:
            if spec.param_support.top_k == "native":
                kwargs["top_k"] = top_k
            else:
                logger.warning(
                    "llm_factory: 剥离 top_k 参数（模型 {} 声明的通道 {} 不支持）",
                    spec.id,
                    spec.param_support.top_k,
                )

    return ChatAnthropic(**kwargs)
