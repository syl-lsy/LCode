"""ModelRegistry：config/models.yaml 的加载、校验与查询（A3 §2.3/§3.2；M2 任务 3）。

职责边界：
- **加载**：以 `Path(__file__)` 锚定 `apps/server/config/models.yaml` 绝对路径（不依赖
  cwd，规避基座铁律 6 同类坑）；模块导入即加载并校验一次（单例 `model_registry`）——
  缺字段/未知角色/未知模型引用直接 fail-fast，把配置错误挡在启动期；
- **resolve**：优先级链 请求级 > 会话级 > 角色级 > `gateway.default_model`
  （AC-014-1/2）；产出 `ResolvedModel`（模型规格 + 有效 effort），参数剥离与方言
  翻译不在这里做（那是 llm_factory 的行为面，注册表只报"声明了什么"）；
- **list_models**：直出契约 `ModelList`（`reasoning_efforts` 按 `supports_reasoning`
  过滤，非 reasoning 模型恒空表——AC-014-3 前端置灰依据）；
- **pricing**：给 cost_service 的单价查询；未知模型 id → `ModelNotFound`
  （契约 MODEL_NOT_FOUND 404）。

注意：`config/` 不进 wheel 打包（hatch 只收 `app/`），服务按仓库内运行口径使用；
若未来容器化，需把 `config/models.yaml` 一并装入镜像同路径。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from app.exception.model import ModelNotFound
from lcode_contracts.common import EffortLevel
from lcode_contracts.rest import ModelInfo, ModelList

# ---------------------------------------------------------------- 类型与常量

Role = Literal[
    "main", "explorer", "coder", "reviewer", "router", "summarizer", "approval_classifier"
]
"""角色路由的合法集（AGENTS.md 接缝表 get_llm(role)；新增须同步 models.yaml roles 表）。"""

KNOWN_ROLES: tuple[str, ...] = (
    "main",
    "explorer",
    "coder",
    "reviewer",
    "router",
    "summarizer",
    "approval_classifier",
)
_KNOWN_ROLE_SET = frozenset(KNOWN_ROLES)

_EFFORT_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")
_EFFORT_SET = frozenset(_EFFORT_LEVELS)

MIN_THINKING_BUDGET = 1024
"""Anthropic Messages 扩展思考预算下限：budget_tokens 必须 >= 本值（网关侧硬约束）。"""

MODELS_YAML_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
"""models.yaml 绝对路径（apps/server/config/），加载与 cwd 无关（A3 §2.3）。"""


class ModelsConfigError(ValueError):
    """models.yaml 结构/引用校验失败（启动 fail-fast；A3 §2.3）。"""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """单价（元 / 百万 token 口径，cost_service 消费）。"""

    input_per_mtok: float
    output_per_mtok: float
    currency: str


@dataclass(frozen=True, slots=True)
class ParamSupport:
    """参数支持面（A3 §3.3；ADR-007 的声明源）。

    - temperature：是否接受 temperature（thinking 模型为 False，工厂剥离；
      且 thinking 启用时无论声明与否一律剥离——扩展思考要求温度语义固定）；
    - top_k：Anthropic Messages 顶层标准参数，当前唯一声明值 `native`
      （ChatOpenAI 的 extra_body 口径已随 ADR-007 移除）——注解收窄为
      Literal["native"]（F10），让 mypy 拦住把它当自由字符串传递的调用方。
    """

    temperature: bool
    top_k: Literal["native"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """单个模型的注册规格（yaml models 表一行的强类型投影）。"""

    id: str
    display_name: str
    provider: str
    supports_reasoning: bool
    reasoning_efforts: tuple[EffortLevel, ...]
    param_support: ParamSupport
    effort_dialect: MappingProxyType[str, MappingProxyType[str, Any]] | None
    context_window: int
    max_output_tokens: int
    pricing: ModelPricing


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """角色路由一行：默认模型 + 默认档位（均可被上层覆盖）。"""

    model: str | None = None  # 缺省 → gateway.default_model（优先级链角色级空位）
    reasoning_effort: EffortLevel | None = None


@dataclass(frozen=True, slots=True)
class SessionModelCfg:
    """会话级覆盖（sessions.model / sessions.reasoning_effort 两列的内存投影）。"""

    model: str | None = None
    reasoning_effort: EffortLevel | None = None


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """resolve 结果：模型规格 + 沿优先级链取到的有效 effort（未声明为 None）。"""

    role: str
    spec: ModelSpec
    effort: EffortLevel | None


# ---------------------------------------------------------------- 注册表


class ModelRegistry:
    """models.yaml 的内存注册表（构造即校验，fail-fast；A3 §2.3）。"""

    def __init__(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise ModelsConfigError("models.yaml 顶层必须是映射 {gateway, roles, models}")
        for section in ("gateway", "roles", "models"):
            if not isinstance(data.get(section), dict):
                raise ModelsConfigError(f"models.yaml 缺顶层段 {section}（或非映射）")
        self._protocol = self._build_protocol(data["gateway"])
        self._default_model = self._build_default_model(data["gateway"])
        self._models = self._build_models(data["models"])
        if self._default_model not in self._models:
            raise ModelsConfigError(
                f"gateway.default_model={self._default_model!r} 未在 models 表中定义"
            )
        self._roles = self._build_roles(data["roles"])

    # ---- 构造与校验（私有，全部 fail-fast 且点名错因） ----

    def _build_protocol(self, gateway: dict[str, Any]) -> str:
        protocol = gateway.get("protocol", "anthropic")
        if protocol != "anthropic":
            raise ModelsConfigError(
                f"gateway.protocol={protocol!r} 不支持（当前仅 'anthropic'："
                "Anthropic Messages 兼容网关，ADR-007）"
            )
        return str(protocol)

    def _build_default_model(self, gateway: dict[str, Any]) -> str:
        default_model = gateway.get("default_model")
        if not isinstance(default_model, str) or not default_model:
            raise ModelsConfigError("gateway.default_model 缺失（须为非空字符串）")
        return default_model

    def _build_models(self, models: dict[str, Any]) -> dict[str, ModelSpec]:
        if not models:
            raise ModelsConfigError("models 表为空：至少注册一个模型")
        built: dict[str, ModelSpec] = {}
        for model_id, raw in models.items():
            if not isinstance(raw, dict):
                raise ModelsConfigError(f"models.{model_id} 须为映射")
            built[model_id] = self._build_model_spec(str(model_id), raw)
        return built

    def _build_model_spec(self, model_id: str, raw: dict[str, Any]) -> ModelSpec:
        missing = [
            field
            for field in (
                "display_name",
                "provider",
                "supports_reasoning",
                "reasoning_efforts",
                "param_support",
                "context_window",
                "max_output_tokens",
                "pricing",
            )
            if field not in raw
        ]
        if missing:
            raise ModelsConfigError(f"models.{model_id} 缺字段：{', '.join(missing)}")

        supports_reasoning = raw["supports_reasoning"]
        if not isinstance(supports_reasoning, bool):
            raise ModelsConfigError(f"models.{model_id}.supports_reasoning 须为布尔")
        efforts = raw["reasoning_efforts"]
        if not isinstance(efforts, list) or any(e not in _EFFORT_SET for e in efforts):
            raise ModelsConfigError(
                f"models.{model_id}.reasoning_efforts 只能为 {list(_EFFORT_LEVELS)} 的子集列表"
            )
        if not supports_reasoning and efforts:
            raise ModelsConfigError(
                f"models.{model_id} supports_reasoning=false 时 reasoning_efforts 必须为空表"
                "（AC-014-3 置灰依据）"
            )
        if supports_reasoning and not efforts:
            raise ModelsConfigError(
                f"models.{model_id} supports_reasoning=true 时须至少声明一个支持档位"
            )

        param_support = raw["param_support"]
        if not isinstance(param_support, dict) or "temperature" not in param_support:
            raise ModelsConfigError(f"models.{model_id}.param_support 须含 temperature")
        if not isinstance(param_support["temperature"], bool):
            raise ModelsConfigError(f"models.{model_id}.param_support.temperature 须为布尔")
        top_k_mode = param_support.get("top_k", "native")
        if top_k_mode != "native":
            raise ModelsConfigError(
                f"models.{model_id}.param_support.top_k 当前只支持 'native'"
                "（top_k 是 Anthropic Messages 顶层标准参数；extra_body 口径已随"
                " ChatOpenAI 移除，ADR-007）"
            )

        pricing_raw = raw["pricing"]
        if not isinstance(pricing_raw, dict):
            raise ModelsConfigError(f"models.{model_id}.pricing 须为映射")
        try:
            pricing = ModelPricing(
                input_per_mtok=float(pricing_raw["input_per_mtok"]),
                output_per_mtok=float(pricing_raw["output_per_mtok"]),
                currency=str(pricing_raw["currency"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelsConfigError(
                f"models.{model_id}.pricing 须含 input_per_mtok/output_per_mtok/currency"
            ) from exc
        if pricing.input_per_mtok < 0 or pricing.output_per_mtok < 0:
            raise ModelsConfigError(f"models.{model_id}.pricing 单价不能为负")

        context_window = raw["context_window"]
        if not isinstance(context_window, int) or context_window <= 0:
            raise ModelsConfigError(f"models.{model_id}.context_window 须为正整数")

        max_output_tokens = raw["max_output_tokens"]
        # bool 是 int 子类，显式排除（True 会伪装成正整数）
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or (max_output_tokens <= 0)
        ):
            raise ModelsConfigError(
                f"models.{model_id}.max_output_tokens 须为正整数"
                "（Anthropic Messages 必需参数，缺省会落默认 1024 静默截断输出——ADR-007）"
            )

        dialect_raw = raw.get("effort_dialect")
        dialect: MappingProxyType[str, MappingProxyType[str, Any]] | None = None
        if supports_reasoning:
            if not isinstance(dialect_raw, dict):
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect 缺失：reasoning 模型须给出"
                    "档位 → Anthropic thinking 参数的翻译表（ADR-007）"
                )
            uncovered = set(efforts) - set(dialect_raw)
            if uncovered:
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect 未覆盖已声明档位：{sorted(uncovered)}"
                )
            self._validate_dialect_entries(model_id, dialect_raw, max_output_tokens)
            dialect = MappingProxyType({e: MappingProxyType(dict(dialect_raw[e])) for e in efforts})
        elif dialect_raw is not None:
            raise ModelsConfigError(
                f"models.{model_id} supports_reasoning=false 不应声明 effort_dialect"
            )

        return ModelSpec(
            id=model_id,
            display_name=str(raw["display_name"]),
            provider=str(raw["provider"]),
            supports_reasoning=supports_reasoning,
            reasoning_efforts=tuple(efforts),
            param_support=ParamSupport(
                temperature=param_support["temperature"],
                # top_k_mode 已在上方守卫中限定为 "native"（extra_body 口径随 ADR-007 移除）
                top_k="native",
            ),
            effort_dialect=dialect,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            pricing=pricing,
        )

    @staticmethod
    def _validate_dialect_entries(
        model_id: str, dialect_raw: dict[str, Any], max_output_tokens: int
    ) -> None:
        """档位 → Anthropic thinking 参数的形态校验（ADR-007）。

        - 空表：仅 "off" 档允许（语义 = 不传 thinking；开档空表无意义）；
        - 非空 entry：必须恰为 {"type": "enabled", "budget_tokens": <int>}，且
          MIN_THINKING_BUDGET(1024) <= budget_tokens < max_output_tokens（Anthropic
          扩展思考下限 + 预算严格小于 max_tokens 留输出余量；注册表在此拦越界，
          工厂另有防御 clamp）。注：跨档单调性不在注册表校验范围（U1 spike 校准
          可能引入非单调的合法形态），由 test_llm_factory 的方言用例锁定。
        """

        for effort_key, entry in dialect_raw.items():
            if not isinstance(entry, Mapping):
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect.{effort_key} 须为映射"
                    "（Anthropic thinking 形态，ADR-007）"
                )
            if not entry:
                if effort_key != "off":
                    raise ModelsConfigError(
                        f"models.{model_id}.effort_dialect.{effort_key} 为空表："
                        "仅 'off' 档允许空表（空 = 不传 thinking；开档空表语义非法）"
                    )
                continue
            budget = entry.get("budget_tokens")
            shape_ok = (
                set(entry) == {"type", "budget_tokens"}
                and entry.get("type") == "enabled"
                and isinstance(budget, int)
                and not isinstance(budget, bool)
            )
            if not shape_ok:
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect.{effort_key} 须恰为"
                    " {'type': 'enabled', 'budget_tokens': <int>}"
                    "（Anthropic thinking 形态，ADR-007）"
                )
            assert isinstance(budget, int)  # shape_ok 已收窄；mypy 不追踪跨变量类型流
            if budget < MIN_THINKING_BUDGET:
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect.{effort_key}.budget_tokens={budget}"
                    f" 低于下限：Anthropic Messages 要求 budget_tokens >= {MIN_THINKING_BUDGET}"
                )
            if budget >= max_output_tokens:
                raise ModelsConfigError(
                    f"models.{model_id}.effort_dialect.{effort_key}.budget_tokens={budget}"
                    f" 越界：须 {MIN_THINKING_BUDGET} <= budget_tokens < "
                    f"max_output_tokens({max_output_tokens})"
                )

    def _build_roles(self, roles: dict[str, Any]) -> dict[str, RoleSpec]:
        unknown = set(roles) - _KNOWN_ROLE_SET
        if unknown:
            raise ModelsConfigError(
                f"roles 含未知角色 {sorted(unknown)}（合法角色：{list(KNOWN_ROLES)}）"
            )
        missing = set(KNOWN_ROLES) - set(roles)
        if missing:
            raise ModelsConfigError(f"roles 缺角色 {sorted(missing)}（7 角色必须齐全）")
        built: dict[str, RoleSpec] = {}
        for role, raw in roles.items():
            if not isinstance(raw, dict):
                raise ModelsConfigError(f"roles.{role} 须为映射")
            model_id = raw.get("model")
            if model_id is not None:
                if not isinstance(model_id, str):
                    raise ModelsConfigError(f"roles.{role}.model 须为字符串")
                if model_id not in self._models:
                    raise ModelsConfigError(
                        f"角色 {role!r} 引用未知模型 {model_id!r}（未在 models 表定义）"
                    )
            effort = raw.get("reasoning_effort")
            if effort is not None:
                if effort not in _EFFORT_SET:
                    raise ModelsConfigError(
                        f"roles.{role}.reasoning_effort={effort!r} 非法"
                        f"（合法：{list(_EFFORT_LEVELS)}）"
                    )
                spec = self._models.get(model_id) if model_id else None
                if spec is not None and not spec.supports_reasoning:
                    raise ModelsConfigError(
                        f"roles.{role} 给了档位但模型 {model_id!r} 不支持 reasoning"
                    )
                if spec is not None and effort not in spec.reasoning_efforts:
                    raise ModelsConfigError(
                        f"roles.{role}.reasoning_effort={effort!r} 不在模型 {model_id!r}"
                        f" 的支持档位 {list(spec.reasoning_efforts)} 内"
                    )
            built[role] = RoleSpec(
                model=model_id,
                reasoning_effort=effort,
            )
        return built

    # ---- 加载 ----

    @classmethod
    def load(cls, path: Path | None = None) -> ModelRegistry:
        """从 yaml 加载（默认 MODELS_YAML_PATH，Path(__file__) 锚定，与 cwd 无关）。"""

        yaml_path = path or MODELS_YAML_PATH
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ModelsConfigError(f"models.yaml 不存在：{yaml_path}") from exc
        except yaml.YAMLError as exc:
            raise ModelsConfigError(f"models.yaml 解析失败：{exc}") from exc
        return cls(data)

    # ---- 只读视图 ----

    @property
    def protocol(self) -> str:
        """网关协议（gateway.protocol）：当前恒 "anthropic"（ADR-007）。"""
        return self._protocol

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def roles(self) -> MappingProxyType[str, RoleSpec]:
        return MappingProxyType(self._roles)

    @property
    def models(self) -> MappingProxyType[str, ModelSpec]:
        return MappingProxyType(self._models)

    # ---- 查询（A3 §3.1/§3.2） ----

    def resolve(
        self,
        role: str,
        *,
        model_override: str | None = None,
        effort_override: EffortLevel | None = None,
        session_cfg: SessionModelCfg | None = None,
    ) -> ResolvedModel:
        """优先级链：请求级 > 会话级 > 角色级 > gateway.default_model（AC-014-1/2）。

        只做"选模型 + 选档位"；param_support 剥离与 effort 方言翻译在 llm_factory。
        空串 override / 会话字段**视为未提供**（falsy 静默落到下一层，不报错）——
        上层传入前应自行清洗，本方法不做空串哨兵。
        """

        if role not in _KNOWN_ROLE_SET:
            raise ValueError(f"未知角色 {role!r}（合法角色：{list(KNOWN_ROLES)}）")
        role_spec = self._roles[role]
        session_model = session_cfg.model if session_cfg is not None else None
        model_id = model_override or session_model or role_spec.model or self._default_model
        spec = self.spec(model_id)  # 未知模型 → ModelNotFound
        session_effort = session_cfg.reasoning_effort if session_cfg is not None else None
        effort: EffortLevel | None = effort_override or session_effort or role_spec.reasoning_effort
        if effort is not None and effort not in _EFFORT_SET:
            raise ValueError(f"effort={effort!r} 非法（合法：{list(_EFFORT_LEVELS)}）")
        return ResolvedModel(role=role, spec=spec, effort=effort)

    def spec(self, model_id: str) -> ModelSpec:
        """按 id 取模型规格；未知 id → ModelNotFound（契约 MODEL_NOT_FOUND 404）。"""

        try:
            return self._models[model_id]
        except KeyError:
            raise ModelNotFound(
                f"模型不存在：{model_id}",
                detail=f"models.yaml 注册表无此 id（可用：{list(self._models)}）",
            ) from None

    def pricing(self, model_id: str) -> ModelPricing:
        """单价查询（cost_service 消费）；未知 id → ModelNotFound。"""

        return self.spec(model_id).pricing

    def list_models(self) -> ModelList:
        """直出契约 ModelList：reasoning_efforts 按 supports_reasoning 过滤（AC-014-1/3）。"""

        infos = [
            ModelInfo(
                id=m.id,
                display_name=m.display_name,
                provider=m.provider,
                supports_reasoning=m.supports_reasoning,
                reasoning_efforts=list(m.reasoning_efforts) if m.supports_reasoning else [],
                is_default=(m.id == self._default_model),
            )
            for m in self._models.values()
        ]
        return ModelList(models=infos, default_model=self._default_model)


# 模块级单例：导入即加载并校验（fail-fast 在启动期，A3 §2.3）
model_registry: ModelRegistry = ModelRegistry.load()
