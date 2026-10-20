"""unit：ModelRegistry（A3 §2.3/§3.2；ADR-007；M2 任务 3）。

覆盖面（test-plan §2.5 REQ-014 unit 列 + fail-fast 校验）：
- models.yaml 以 `Path(__file__)` 锚定加载，与运行 cwd 无关（基座铁律 6 同类坑规避）；
- 启动校验 fail-fast：未知角色/未知模型引用/缺字段/方言覆盖不全 → 明确异常；
- gateway.protocol：默认 "anthropic"、仅接受 "anthropic"（ADR-007，属性暴露）；
- ModelSpec.max_output_tokens：正整数必填（Anthropic Messages 必需参数的声明源）；
- param_support.top_k 仅接受 "native"（Anthropic 顶层；extra_body 口径随 ChatOpenAI 移除）；
- effort_dialect 形态校验：非空 entry 须为 {"type": "enabled", "budget_tokens": int}
  且 0 < budget < max_output_tokens；仅 "off" 允许空表；覆盖全部声明档位；
- resolve 优先级链：请求级 > 会话级 > 角色级 > gateway.default_model（AC-014-1/2）；
- 角色默认路由（AC-014-4：旗舰/经济档分档）；
- list_models() 直出契约 ModelList/ModelInfo（reasoning_efforts 按 supports_reasoning 过滤）；
- pricing(model_id) 查询，未知模型 → ModelNotFound（契约 MODEL_NOT_FOUND 404）。

单测零网络：全部基于真实 yaml 与内存构造的篡改配置。
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from app.agent.models_registry import (
    KNOWN_ROLES,
    ModelRegistry,
    SessionModelCfg,
)
from app.exception.model import ModelNotFound
from lcode_contracts.rest import ModelList

# ---------------------------------------------------------------- 构造篡改配置的工具


def raw_config(**overrides: Any) -> dict[str, Any]:
    """最小合法配置骨架（结构与 A3 §3.2 / ADR-007 一致），供逐项篡改验证 fail-fast。

    7 角色必须齐全（注册表启动校验规则）：旗舰档 effort 齐备、经济档无 effort。
    """

    models: dict[str, Any] = {
        "flagship": {
            "display_name": "Flagship",
            "provider": "bailian",
            "supports_reasoning": True,
            "reasoning_efforts": ["off", "low", "medium", "high"],
            "param_support": {"temperature": False, "top_k": "native"},
            "max_output_tokens": 32768,
            "effort_dialect": {
                "off": {},
                "low": {"type": "enabled", "budget_tokens": 1024},
                "medium": {"type": "enabled", "budget_tokens": 2048},
                "high": {"type": "enabled", "budget_tokens": 4096},
            },
            "context_window": 262144,
            "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "currency": "CNY"},
        },
        "economy": {
            "display_name": "Economy",
            "provider": "bailian",
            "supports_reasoning": False,
            "reasoning_efforts": [],
            "param_support": {"temperature": True, "top_k": "native"},
            "max_output_tokens": 8192,
            "context_window": 131072,
            "pricing": {"input_per_mtok": 0.1, "output_per_mtok": 0.2, "currency": "CNY"},
        },
    }
    roles: dict[str, Any] = {
        "main": {"model": "flagship", "reasoning_effort": "high"},
        "coder": {"model": "flagship", "reasoning_effort": "high"},
        "reviewer": {"model": "flagship", "reasoning_effort": "medium"},
        "explorer": {"model": "flagship", "reasoning_effort": "low"},
        "router": {"model": "economy"},
        "summarizer": {"model": "economy"},
        "approval_classifier": {"model": "economy"},
    }
    data: dict[str, Any] = {
        "gateway": {"default_model": "flagship"},
        "roles": roles,
        "models": models,
    }
    for dotted, value in overrides.items():
        node: Any = data
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return data


# ---------------------------------------------------------------- 加载与启动校验


def test_load_reads_service_local_yaml_via_path_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """加载与 cwd 无关：切到任意目录后 load() 仍读到 apps/server/config/models.yaml（A3 §2.3）。"""

    monkeypatch.chdir(tmp_path)  # 若实现依赖 cwd（如 Path("config/models.yaml")）这里就红
    registry = ModelRegistry.load()
    assert registry.list_models().default_model == "qwen3.7-plus"
    # 断言真正的落点：锚定路径在服务目录内（apps/server/config/）
    assert registry is not None


def test_real_yaml_declares_all_seven_roles_and_three_models():
    """真实 yaml：7 角色齐全 + 至少 3 个模型 + 结构字段齐全（A3 §3.2）。"""

    registry = ModelRegistry.load()
    assert set(registry.roles) == set(KNOWN_ROLES)
    for role in KNOWN_ROLES:
        assert registry.roles[role].model in registry.models
    for expected in ("qwen3.7-plus", "qwen3.8-flash", "qwen3.6-flash"):
        assert expected in registry.models, f"缺基线模型 {expected}"
    # U1 固化锚（test-plan §6.6）：thinking 方言必须显式成表且覆盖全部档位，
    # 值标 provisional——升级 yaml 时本用例即回归锚，防"改文档忘改方言表"。
    dialect = registry.models["qwen3.7-plus"].effort_dialect
    assert dialect is not None and set(dialect) == {"off", "low", "medium", "high"}


def test_real_yaml_anthropic_baseline():
    """真实 yaml 的 ADR-007 基线：protocol/max_output_tokens/方言预算逐项锁值。"""

    registry = ModelRegistry.load()
    assert registry.protocol == "anthropic"
    max_spec = registry.models["qwen3.7-plus"]
    plus_spec = registry.models["qwen3.8-flash"]
    flash_spec = registry.models["qwen3.6-flash"]
    assert max_spec.max_output_tokens == 131072  # 官方模型页
    assert plus_spec.max_output_tokens == 131072  # 官方模型页
    assert flash_spec.max_output_tokens == 65536  # 官方模型页（2026-10-20 核对）
    # 官方 context 规格（2026-10-20 核对：三模型均为 1M）
    assert max_spec.context_window == 1000000
    assert plus_spec.context_window == 1000000
    assert flash_spec.context_window == 1000000
    assert flash_spec.effort_dialect is None  # flash 无方言
    assert max_spec.param_support.top_k == "native"
    assert plus_spec.param_support.top_k == "native"
    assert flash_spec.param_support.top_k == "native"
    dialect = max_spec.effort_dialect
    assert dialect is not None
    assert dialect["off"] == {}
    assert dialect["low"]["budget_tokens"] == 2048
    assert dialect["medium"]["budget_tokens"] == 8192
    assert dialect["high"]["budget_tokens"] == 31744  # < 131072，余量充足


def test_role_referencing_unknown_model_fails_fast():
    data = raw_config(**{"roles.main.model": "no-such-model"})
    with pytest.raises(ValueError, match="no-such-model"):
        ModelRegistry(data)


def test_unknown_role_key_in_yaml_fails_fast():
    data = raw_config(**{"roles.mian": {"model": "flagship"}})  # 拼写错位：mian
    with pytest.raises(ValueError, match="mian"):
        ModelRegistry(data)


def test_missing_required_model_field_fails_fast():
    data = raw_config()
    del data["models"]["flagship"]["pricing"]
    with pytest.raises(ValueError, match="pricing"):
        ModelRegistry(data)


def test_default_model_must_exist():
    data = raw_config(**{"gateway.default_model": "ghost"})
    with pytest.raises(ValueError, match="ghost"):
        ModelRegistry(data)


def test_role_effort_outside_model_efforts_fails_fast():
    data = raw_config(**{"roles.router.reasoning_effort": "high"})  # economy 不支持档位
    with pytest.raises(ValueError, match="router"):
        ModelRegistry(data)


def test_non_reasoning_model_with_efforts_listed_fails_fast():
    data = raw_config()
    data["models"]["economy"]["reasoning_efforts"] = ["low"]
    with pytest.raises(ValueError, match="reasoning_efforts"):
        ModelRegistry(data)


def test_dialect_must_cover_declared_efforts():
    data = raw_config()
    data["models"]["flagship"]["effort_dialect"].pop("high")
    with pytest.raises(ValueError, match="high"):
        ModelRegistry(data)


# ---------------------------------------------------------------- protocol / max_output_tokens / top_k（ADR-007）


def test_protocol_defaults_to_anthropic():
    """gateway.protocol 未声明 → 默认 "anthropic"，属性暴露。"""

    registry = ModelRegistry(raw_config())
    assert registry.protocol == "anthropic"


def test_protocol_rejects_non_anthropic():
    data = raw_config(**{"gateway.protocol": "openai"})
    with pytest.raises(ValueError, match="anthropic"):
        ModelRegistry(data)


def test_max_output_tokens_required_and_positive():
    """max_output_tokens 必填且为正整数（Anthropic Messages 必需参数的声明源）。"""

    data = raw_config()
    del data["models"]["flagship"]["max_output_tokens"]
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelRegistry(data)

    data = raw_config(**{"models.flagship.max_output_tokens": 0})
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelRegistry(data)

    data = raw_config(**{"models.flagship.max_output_tokens": -1})
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelRegistry(data)


def test_top_k_must_be_native():
    """param_support.top_k 仅接受 "native"（extra_body 口径随 ChatOpenAI 移除，ADR-007）。"""

    data = raw_config(
        **{"models.flagship.param_support": {"temperature": False, "top_k": "extra_body"}}
    )
    with pytest.raises(ValueError, match="native"):
        ModelRegistry(data)


def test_param_support_top_k_annotation_is_native_literal():
    """F10：top_k 的 Python 注解收窄为 Literal["native"]（不再是裸 str）。

    extra_body 口径已随 ADR-007 移除，声明值域只有一个——注解如实收窄，让 mypy
    拦住任何把 top_k 当自由字符串传递的调用方。
    """
    import typing

    from app.agent.models_registry import ParamSupport

    hints = typing.get_type_hints(ParamSupport)
    assert hints["top_k"] == typing.Literal["native"]


# ---------------------------------------------------------------- effort_dialect 形态校验（ADR-007）


def test_dialect_entry_must_be_enabled_shape():
    """非空 entry 须为 {"type": "enabled", "budget_tokens": int}，错误点名模型与档位。"""

    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["high"] = {"enable_thinking": True}
    with pytest.raises(ValueError, match="flagship.*high"):
        ModelRegistry(data)


def test_dialect_budget_must_be_int():
    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["low"] = {
        "type": "enabled",
        "budget_tokens": "1024",
    }
    with pytest.raises(ValueError, match="flagship.*low"):
        ModelRegistry(data)


def test_dialect_budget_must_be_positive():
    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["low"] = {"type": "enabled", "budget_tokens": 0}
    with pytest.raises(ValueError, match="budget_tokens"):
        ModelRegistry(data)


def test_dialect_budget_must_meet_anthropic_lower_bound():
    """budget_tokens >= 1024（Anthropic Messages 扩展思考下限），错误点名模型与档位。"""

    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["low"] = {
        "type": "enabled",
        "budget_tokens": 512,  # > 0 但低于 Anthropic 下限
    }
    with pytest.raises(ValueError, match="flagship.*low"):
        ModelRegistry(data)


def test_dialect_budget_must_be_below_max_output_tokens():
    """budget >= max_output_tokens 越界（Anthropic 要求 budget < max_tokens，留输出余量）。"""

    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["high"] = {
        "type": "enabled",
        "budget_tokens": 32768,  # == max_output_tokens，不留输出余量
    }
    with pytest.raises(ValueError, match="budget_tokens"):
        ModelRegistry(data)


def test_only_off_may_be_empty_dict():
    """ "off" 允许空表（不传 thinking）；开档空表语义非法，错误点名模型与档位。"""

    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["off"] = {}
    ModelRegistry(raw_config())  # 真实骨架的 off 即空表：合法

    data = raw_config()
    data["models"]["flagship"]["effort_dialect"]["low"] = {}
    with pytest.raises(ValueError, match="flagship.*low"):
        ModelRegistry(data)


# ---------------------------------------------------------------- resolve 优先级链


def test_role_priority_chain():
    """AC-014-1：请求级 > 会话级 > 角色级 > default_model 四层逐级回退。"""

    registry = ModelRegistry.load()

    # 角色级：main = qwen3.7-plus + high（yaml roles 表）
    resolved = registry.resolve("main")
    assert resolved.spec.id == "qwen3.7-plus"
    assert resolved.effort == "high"

    # default_model 兜底：yaml 里 router 只声明 model，无 effort → effort=None
    resolved = registry.resolve("router")
    assert resolved.spec.id == "qwen3.6-flash"
    assert resolved.effort is None

    # 会话级：会话行覆盖角色默认
    resolved = registry.resolve(
        "main",
        session_cfg=SessionModelCfg(model="qwen3.8-flash", reasoning_effort="low"),
    )
    assert resolved.spec.id == "qwen3.8-flash"
    assert resolved.effort == "low"

    # 请求级：参数位最高（P0 无 UI 来源，为 API/内部调用预留）
    resolved = registry.resolve(
        "main",
        model_override="qwen3.6-flash",
        effort_override="off",
        session_cfg=SessionModelCfg(model="qwen3.8-flash", reasoning_effort="low"),
    )
    assert resolved.spec.id == "qwen3.6-flash"
    assert resolved.effort == "off"


def test_request_over_session_precedence():
    """AC-014-2：请求级与会话级同时给出时请求级胜出。"""

    registry = ModelRegistry.load()
    resolved = registry.resolve(
        "explorer",
        model_override="qwen3.7-plus",
        effort_override="high",
        session_cfg=SessionModelCfg(model="qwen3.6-flash", reasoning_effort=None),
    )
    assert resolved.spec.id == "qwen3.7-plus"
    assert resolved.effort == "high"


def test_session_partial_override_fills_from_role():
    """会话只带 model 不带 effort：effort 沿角色级继续下探。"""

    registry = ModelRegistry.load()
    resolved = registry.resolve("reviewer", session_cfg=SessionModelCfg(model="qwen3.7-plus"))
    assert resolved.spec.id == "qwen3.7-plus"
    assert resolved.effort == "medium"  # roles.reviewer.reasoning_effort


def test_resolve_unknown_role_or_model_raises():
    registry = ModelRegistry.load()
    with pytest.raises(ValueError, match="unknown-role"):
        registry.resolve("unknown-role")
    with pytest.raises(ModelNotFound):
        registry.resolve("main", model_override="no-such-model")
    with pytest.raises(ModelNotFound):
        registry.resolve("main", session_cfg=SessionModelCfg(model="no-such-model"))


# ---------------------------------------------------------------- 角色默认路由（AC-014-4）


def test_role_default_routing():
    """旗舰档（main/coder/reviewer/explorer）与经济档（router/summarizer/审批分类器）分档。"""

    registry = ModelRegistry.load()
    for role in ("main", "coder"):
        assert registry.resolve(role).spec.id == "qwen3.7-plus"
    for role in ("reviewer", "explorer"):
        assert registry.resolve(role).spec.id == "qwen3.8-flash"
    for role in ("router", "summarizer", "approval_classifier"):
        assert registry.resolve(role).spec.id == "qwen3.6-flash"


def test_baseline_model_ids_locked():
    """基线锁定（模型改名回归锚）：default_model 与关键角色映射显式钉死。"""

    registry = ModelRegistry.load()
    assert registry.list_models().default_model == "qwen3.7-plus"

    expected = {
        "main": "qwen3.7-plus",
        "coder": "qwen3.7-plus",
        "reviewer": "qwen3.8-flash",
        "explorer": "qwen3.8-flash",
        "router": "qwen3.6-flash",
    }
    for role, model_id in expected.items():
        assert registry.resolve(role).spec.id == model_id, f"角色 {role} 解析错误"


# ---------------------------------------------------------------- list_models / pricing


def test_list_models_matches_contract_shape():
    """list_models() 直出契约 ModelList：字段、is_default、reasoning_efforts 过滤。"""

    registry = ModelRegistry.load()
    model_list = registry.list_models()
    assert isinstance(model_list, ModelList)
    TypeAdapter(ModelList).validate_python(model_list.model_dump(mode="json"))

    by_id = {m.id: m for m in model_list.models}
    assert model_list.default_model == "qwen3.7-plus"
    # AC-014-3 置灰依据：非 reasoning 模型的可选档位为空表
    assert by_id["qwen3.6-flash"].supports_reasoning is False
    assert by_id["qwen3.6-flash"].reasoning_efforts == []
    assert by_id["qwen3.7-plus"].supports_reasoning is True
    assert by_id["qwen3.7-plus"].reasoning_efforts == ["off", "low", "medium", "high"]
    # is_default 只落在 default_model 上
    assert [m.id for m in model_list.models if m.is_default] == ["qwen3.7-plus"]


def test_pricing_lookup_and_unknown_model():
    registry = ModelRegistry.load()
    pricing = registry.pricing("qwen3.7-plus")
    assert pricing.currency == "CNY"
    assert pricing.input_per_mtok >= 0 and pricing.output_per_mtok >= 0
    with pytest.raises(ModelNotFound):
        registry.pricing("no-such-model")
