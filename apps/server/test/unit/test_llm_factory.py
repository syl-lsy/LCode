"""unit：get_llm 模型工厂（A3 §3.1/§3.3；ADR-007；M2 任务 3）。

覆盖面（test-plan §2.5 REQ-014 unit 列）：
- 优先级链贯穿到 ChatAnthropic 实例（AC-014-1/2）；
- 角色默认路由（AC-014-4）；
- 构造参数面：model / base_url / timeout / max_retries / max_tokens（Anthropic 必需，
  不落默认 1024）；
- effort 方言 → `thinking` 参数：off/空表不传 thinking，开档 = {"type": "enabled",
  "budget_tokens": N}，与 models.yaml 逐项一致（U1 provisional 锚）；
- 预算防御 clamp：budget >= max_tokens 时钳到 max_tokens - 1024 + warning（注册表层
  已挡，这里测工厂自防）；
- thinking 启用 → 剥离 temperature/top_k + warning（Anthropic 扩展思考要求温度语义固定）；
- 非 thinking：temperature 按 param_support、top_k 走顶层 native（ADR-007 取代硬约束 3
  的 extra_body 口径）；
- base_url/api key 缺失：调用期报错（import 期不炸），错误信息只含变量名不泄漏密钥。

环境约定：LLM_BASE_URL 为空、密钥未 export——本文件除 requires_llm 冒烟外**零网络**，
网关地址与密钥由 autouse fixture 打桩为假值。
"""

import os
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from pydantic import TypeAdapter

from app.agent import llm_factory
from app.agent.llm_factory import get_llm
from app.agent.models_registry import (
    KNOWN_ROLES,
    ModelRegistry,
    Role,
    SessionModelCfg,
)
from app.core.config import llm_settings
from app.main import app
from lcode_contracts.rest import ModelList

FAKE_BASE_URL = "https://llm-gateway.invalid/v1"
FAKE_KEY_ENV = "DASHSCOPE_API_KEY"
FAKE_KEY = "sk-unit-test-fake-not-a-secret"
FAKE_TIMEOUT_S = 123
FAKE_MAX_RETRIES = 1


@pytest.fixture(autouse=True)
def _fake_gateway(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """全文件打桩：假网关 + 假密钥 + 确定的超时/重试，保证零网络、断言确定。

    例外：带 `requires_llm` 标记的真网关用例**不得打桩**——否则请求会被导向假域名
    （`llm-gateway.invalid`）而报连接错误，把真实网关问题掩盖成连接失败。
    """

    if request.node.get_closest_marker("requires_llm") is not None:
        return

    monkeypatch.setattr(llm_settings, "base_url", FAKE_BASE_URL)
    monkeypatch.setattr(llm_settings, "request_timeout_s", FAKE_TIMEOUT_S)
    monkeypatch.setattr(llm_settings, "max_retries", FAKE_MAX_RETRIES)
    monkeypatch.setenv(FAKE_KEY_ENV, FAKE_KEY)


# ---------------------------------------------------------------- 实例类型与构造参数面


def test_returns_chat_anthropic_with_constructor_args():
    """工厂产出 ChatAnthropic；model/base_url/timeout/retries/max_tokens 逐项落位。

    max_tokens 是 Anthropic Messages 的必需参数——缺省 1024 会静默截断输出（ADR-007）。
    """

    llm = get_llm("main")
    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "qwen3.7-plus"
    assert llm.anthropic_api_url == FAKE_BASE_URL  # base_url 入参的落点别名
    assert llm.default_request_timeout == FAKE_TIMEOUT_S  # timeout 入参的落点别名
    assert llm.max_retries == FAKE_MAX_RETRIES
    assert llm.max_tokens == 131072  # models.yaml qwen3.7-plus.max_output_tokens


# ---------------------------------------------------------------- 优先级链 / 角色路由


def test_role_priority_chain():
    """AC-014-1：四层优先级链逐级回退，最终落到 ChatAnthropic.model。"""

    assert get_llm("main").model == "qwen3.7-plus"  # 角色级
    assert get_llm("router").model == "qwen3.6-flash"  # 角色级经济档
    session = SessionModelCfg(model="qwen3.8-flash", reasoning_effort="low")
    assert get_llm("main", session_cfg=session).model == "qwen3.8-flash"  # 会话级
    assert (  # 请求级
        get_llm(
            "main",
            model_override="qwen3.6-flash",
            session_cfg=session,
        ).model
        == "qwen3.6-flash"
    )


def test_request_over_session_precedence():
    """AC-014-2：请求级 effort 压过会话级（off 生效而非会话级 medium）。"""

    llm = get_llm(
        "main",
        effort_override="off",
        session_cfg=SessionModelCfg(model="qwen3.7-plus", reasoning_effort="medium"),
    )
    assert llm.thinking is None  # off：不传 thinking，而非会话级 medium 的 enabled 形态


def test_role_default_routing():
    """AC-014-4：main/coder 旗舰、reviewer/explorer 次旗舰、三经济角色 flash。"""

    routing: list[tuple[Role, str]] = [
        ("main", "qwen3.7-plus"),
        ("coder", "qwen3.7-plus"),
        ("reviewer", "qwen3.8-flash"),
        ("explorer", "qwen3.8-flash"),
        ("router", "qwen3.6-flash"),
        ("summarizer", "qwen3.6-flash"),
        ("approval_classifier", "qwen3.6-flash"),
    ]
    for role, model in routing:
        assert get_llm(role).model == model, f"角色 {role} 路由错误"
    assert set(KNOWN_ROLES) == {
        "main",
        "coder",
        "reviewer",
        "explorer",
        "router",
        "summarizer",
        "approval_classifier",
    }  # 路由表扩角色时本用例提醒同步


# ---------------------------------------------------------------- effort 方言 → thinking（AC-014-3 + U1 provisional）


def test_effort_dialect_translation():
    """档位 → thinking 参数逐项翻译，且**与 yaml effort_dialect 完全一致**（U1 回归锚）。

    models.yaml 的 Anthropic thinking 方言为 provisional（U1 spike 未做）——本用例不锁
    具体数值的"正确性"，只锁"工厂输出 ≡ yaml 表"：spike 校准只改 yaml，工厂零改动。
    """

    registry = llm_factory.model_registry
    dialect = registry.models["qwen3.7-plus"].effort_dialect
    assert dialect is not None
    for effort in ("off", "low", "medium", "high"):
        llm = get_llm("main", effort_override=effort)
        if not dialect[effort]:
            assert llm.thinking is None, f"档位 {effort} 空表应不传 thinking"
        else:
            assert llm.thinking == dialect[effort], f"档位 {effort} 翻译与 yaml 不一致"

    # 语义锚（与 yaml 注释一致）：off 空表；三档开思考且带 budget_tokens
    assert dialect["off"] == {}
    for effort in ("low", "medium", "high"):
        assert dialect[effort]["type"] == "enabled"
        assert isinstance(dialect[effort]["budget_tokens"], int)
    # 三档预算严格递增（provisional 数值可调，单调性不可破）
    assert (
        dialect["low"]["budget_tokens"]
        < dialect["medium"]["budget_tokens"]
        < dialect["high"]["budget_tokens"]
    )
    # 预算必须留出余量：high 预算 < max_output_tokens（注册表校验规则的真实数据复核）
    assert dialect["high"]["budget_tokens"] < registry.models["qwen3.7-plus"].max_output_tokens


def test_no_effort_declared_omits_thinking():
    """角色未声明档位（如 router）：不传 thinking（网关自适应），非 off 才显式关闭。"""

    llm = get_llm("router")
    assert llm.thinking is None


def test_thinking_budget_clamped_when_ge_max_tokens(
    monkeypatch: pytest.MonkeyPatch, log_records: list[Any]
):
    """防御 clamp：budget >= max_tokens 时钳到 max(1024, max_tokens - 1024) + warning。

    注册表层已拒绝越界配置（见 test_models_registry），本用例用手工 ModelSpec 直塞
    工厂（模拟"配置层被绕过"的纵深防御路径）。两个场景：
    - 常规：max_tokens=32768 → 钳到 31744（留 1024 余量）；
    - 小 max_tokens：max_tokens=1536 → max_tokens-1024=512 低于 Anthropic 扩展思考
      下限，钳到 1024（评审 P2 修复锚：clamp 结果不得 < 1024）。
    """

    from types import SimpleNamespace

    from app.agent.models_registry import ParamSupport, ResolvedModel

    spec = SimpleNamespace(
        id="handcrafted",
        supports_reasoning=True,
        reasoning_efforts=("high",),
        param_support=ParamSupport(temperature=False, top_k="native"),
        effort_dialect={"high": {"type": "enabled", "budget_tokens": 40000}},
        max_output_tokens=32768,
    )

    class _StubRegistry:
        def resolve(self, role: str, **_: Any) -> ResolvedModel:
            return ResolvedModel(role=role, spec=spec, effort="high")  # type: ignore[arg-type]

    monkeypatch.setattr(llm_factory, "model_registry", _StubRegistry())
    llm = get_llm("main", model_override=None)
    assert llm.thinking is not None
    assert llm.thinking["budget_tokens"] == 32768 - 1024  # clamp 到 max_tokens - 1024
    warnings = [r for r in log_records if r["level"].name == "WARNING" and "clamp" in r["message"]]
    assert warnings, "预算 clamp 必须产生 warning（配置层应已拦截，工厂防御要有痕迹）"


def test_thinking_budget_clamp_floors_at_anthropic_minimum(
    monkeypatch: pytest.MonkeyPatch, log_records: list[Any]
):
    """max_tokens <= 2048 时 clamp 公式 max_tokens-1024 会跌破下限：结果须不低于 1024。"""

    from types import SimpleNamespace

    from app.agent.models_registry import ParamSupport, ResolvedModel

    spec = SimpleNamespace(
        id="handcrafted-small",
        supports_reasoning=True,
        reasoning_efforts=("high",),
        param_support=ParamSupport(temperature=False, top_k="native"),
        effort_dialect={"high": {"type": "enabled", "budget_tokens": 40000}},
        max_output_tokens=1536,  # 1536 - 1024 = 512 < 1024
    )

    class _StubRegistry:
        def resolve(self, role: str, **_: Any) -> ResolvedModel:
            return ResolvedModel(role=role, spec=spec, effort="high")  # type: ignore[arg-type]

    monkeypatch.setattr(llm_factory, "model_registry", _StubRegistry())
    llm = get_llm("main", model_override=None)
    assert llm.thinking is not None
    assert llm.thinking["budget_tokens"] == 1024  # 下限兜底，不得产出 < 1024 的预算
    warnings = [r for r in log_records if r["level"].name == "WARNING" and "clamp" in r["message"]]
    assert warnings


# ---------------------------------------------------------------- thinking 启用时的参数剥离


def test_thinking_enabled_strips_temperature_and_top_k(log_records: list[Any]):
    """thinking 启用：temperature/top_k 一律剥离 + warning（扩展思考要求温度语义固定）。

    即便模型声明支持 temperature（此处 main 默认档 high 即开 thinking），也不透传。
    """

    llm = get_llm("main", temperature=0.7, top_k=20)  # main 默认 effort=high → thinking
    assert llm.thinking is not None
    assert llm.temperature is None
    assert llm.top_k is None
    messages = [(r["level"].name, r["message"]) for r in log_records]
    assert any(lvl == "WARNING" and "temperature" in msg for lvl, msg in messages), (
        f"缺 temperature 剥离告警：{messages}"
    )
    assert any(lvl == "WARNING" and "top_k" in msg for lvl, msg in messages), (
        f"缺 top_k 剥离告警：{messages}"
    )


# ---------------------------------------------------------------- 非 thinking 的 param_support（ADR-007 口径）


def test_temperature_stripped_when_param_support_false(log_records: list[Any]):
    """非 thinking + param_support.temperature=false：剥离 + warning（原硬约束 4 语义）。"""

    llm = get_llm("main", temperature=0.7, effort_override="off")  # off：不开 thinking
    assert llm.thinking is None
    assert llm.temperature is None
    warnings = [
        r for r in log_records if r["level"].name == "WARNING" and "temperature" in r["message"]
    ]
    assert warnings


def test_temperature_kept_when_supported(log_records: list[Any]):
    """支持 temperature 的模型（qwen3.6-flash，无 thinking）原样透传，且不产生剥离告警。"""

    llm = get_llm("router", temperature=0.3)
    assert llm.temperature == 0.3
    assert not [r for r in log_records if "temperature" in r["message"]]


def test_top_k_routes_native_top_level(log_records: list[Any]):
    """ADR-007：top_k 走 Anthropic 顶层参数（Messages API 标准字段），非 thinking 时透传。"""

    llm = get_llm("router", top_k=20)  # flash：无 effort → 无 thinking
    assert llm.top_k == 20
    assert not [r for r in log_records if "top_k" in r["message"]]


# ---------------------------------------------------------------- 不支持档位的剥离（AC-014-3）


def test_unsupported_model_effort_noop(log_records: list[Any]):
    """supports_reasoning=false：effort 剥离 no-op + warning（AC-014-3 置灰的后端等价行为）。"""

    llm = get_llm("router", effort_override="high")  # qwen3.6-flash 不支持 reasoning
    assert llm.thinking is None
    warnings = [
        r for r in log_records if r["level"].name == "WARNING" and "effort" in r["message"].lower()
    ]
    assert warnings


def test_effort_not_in_model_supported_list_noop(log_records: list[Any]):
    """reasoning 模型但声明档位表里没有该档：同样剥离 no-op + warning。"""

    data: dict[str, Any] = {
        "gateway": {"default_model": "partial"},
        "roles": {
            "main": {"model": "partial", "reasoning_effort": "low"},
            "coder": {"model": "partial"},
            "reviewer": {"model": "partial"},
            "explorer": {"model": "partial"},
            "router": {"model": "partial"},
            "summarizer": {"model": "partial"},
            "approval_classifier": {"model": "partial"},
        },
        "models": {
            "partial": {
                "display_name": "Partial",
                "provider": "bailian",
                "supports_reasoning": True,
                "reasoning_efforts": ["off", "low"],  # medium/high 未声明
                "param_support": {"temperature": False, "top_k": "native"},
                "max_output_tokens": 4096,
                "effort_dialect": {
                    "off": {},
                    "low": {"type": "enabled", "budget_tokens": 1024},
                },
                "context_window": 1024,
                "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 1.0, "currency": "CNY"},
            }
        },
    }
    monkey_registry = ModelRegistry(data)
    original = llm_factory.model_registry
    llm_factory.model_registry = monkey_registry
    try:
        llm = get_llm("main", effort_override="medium")  # 未声明档位
        assert llm.thinking is None
        warnings = [
            r
            for r in log_records
            if r["level"].name == "WARNING" and "effort" in r["message"].lower()
        ]
        assert warnings
    finally:
        llm_factory.model_registry = original


# ---------------------------------------------------------------- 网关配置缺失（调用期 fail-fast）


def test_missing_base_url_raises_at_call_time_not_import(monkeypatch: pytest.MonkeyPatch):
    """LLM_BASE_URL 为空：import 工厂模块不炸；首次调用给可读错误（点名 Anthropic 网关）。"""

    monkeypatch.setattr(llm_settings, "base_url", "")
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        get_llm("main")


def test_base_url_error_mentions_anthropic_gateway(monkeypatch: pytest.MonkeyPatch):
    """错误文案指明端点形态：Anthropic Messages 兼容网关（/v1/messages），防配错 OpenAI-only 网关。"""

    monkeypatch.setattr(llm_settings, "base_url", "")
    with pytest.raises(RuntimeError, match="Anthropic"):
        get_llm("main")


def test_missing_api_key_error_names_env_var_without_secret(monkeypatch: pytest.MonkeyPatch):
    """密钥缺失：报错点名环境变量名（变量名不敏感），永不出现密钥本体。"""

    monkeypatch.delenv(FAKE_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        get_llm("main")
    message = str(exc_info.value)
    assert FAKE_KEY_ENV in message
    assert FAKE_KEY not in message
    assert "sk-" not in message


def test_error_messages_never_leak_key_value(monkeypatch: pytest.MonkeyPatch):
    """双缺失场景兜底复核：错误信息不含任何 sk- 形态串（NFR-004 日志脱敏同口径）。"""

    monkeypatch.delenv(FAKE_KEY_ENV, raising=False)
    monkeypatch.setattr(llm_settings, "base_url", "")
    with pytest.raises(RuntimeError) as url_err:
        get_llm("main")
    assert "sk-" not in str(url_err.value)


# ---------------------------------------------------------------- GET /models 冒烟（ASGI，零网络）


def test_models_endpoint_contract_shape():
    """AC-014-1 集成锚的 unit 侧：GET /api/v1/models 出信封 + 契约 ModelList。"""

    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/api/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0"
    assert body["message"] == "ok"
    data = TypeAdapter(ModelList).validate_python(body["data"])
    assert data == llm_factory.model_registry.list_models()
    assert isinstance(data, ModelList)


def test_models_endpoint_lists_expected_basline_models():
    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/api/v1/models")
    ids = {m["id"] for m in resp.json()["data"]["models"]}
    assert {"qwen3.7-plus", "qwen3.8-flash", "qwen3.6-flash"} <= ids


# ---------------------------------------------------------------- 真模型冒烟（requires_llm，不进出口门禁）


def _llm_env_ready() -> bool:
    """真实网关可用性：base_url 非空且 api_key_env 指向的变量已有值。"""

    return bool(llm_settings.base_url) and bool(os.environ.get(llm_settings.api_key_env))


@pytest.mark.requires_llm
@pytest.mark.skipif(
    not _llm_env_ready(),
    reason=(
        "真模型冒烟需要 LLM_BASE_URL（.env 的 LLM_BASE_URL=，Anthropic Messages 兼容网关）与 "
        "DASHSCOPE_API_KEY 已 export；缺任一则跳过（显式豁免，非失败——test-plan §1.4）"
    ),
)
def test_real_model_smoke_via_gateway():
    """真网关最小往返：只验证连通与返回非空文本，不锁内容。

    网关可能把思考内容以 block 列表返回（即便经济档未声明 reasoning 档位），
    因此按 block 的 text 段取文本，不假设 `content` 恒为 str。
    """

    llm = get_llm("router")  # 经济档：冒烟成本最低
    reply = llm.invoke("回复一个字：好")
    content = reply.content
    text = (
        content
        if isinstance(content, str)
        else "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    )
    assert text.strip(), "真网关响应无非空文本块"
