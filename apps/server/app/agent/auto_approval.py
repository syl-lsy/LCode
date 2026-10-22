"""REQ-013 AC-013-3/AC-013-4：AUTO 审批分类器 + 决策 middleware（M3 Phase 3）。

**为什么不是「静态摘除 interrupt 面」**：把 `task` 从 `interrupt_on` 里拿掉后，HITL
根本不看这次调用——没有「分类器决策 + reason + 审计行」三个可审计事实，AC-013-3
不成立。本模块采用**两段式**：

1. **分类**发生在模型产出工具调用之后、工具执行之前——挂载点是
   `awrap_model_call`（安装版 langchain 1.4.6 的 model-node 后置钩子）。取证：
   - `langchain/agents/factory.py:1560-1610` 的 `amodel_node` 用
     `awrap_model_call_handler` 包裹模型调用，返回值经 `_build_commands` 落成
     model node 的 state 更新（`ExtendedModelResponse.command` 与模型消息同批提交）；
   - `after_model` 链是**反序**执行的（`factory.py:1856-1866`：`model →
     middleware[-1].after_model → … → middleware[0].after_model`），deepagents 把
     `HumanInTheLoopMiddleware` 追加在栈尾（`deepagents/graph.py:830-835`），所以
     HITL 的 `after_model` 是**第一个**跑的——普通 `after_model` 钩子拿不到「先于
     HITL」的时序。model-node 的 command 更新则先于所有 after_model 节点落库。
2. **决策随状态持久化**（`AUTO_APPROVAL_STATE_KEY`，按 tool_call_id 索引）：字段经
   middleware 的 `state_schema` 进入内层图 state（`factory.py:1226-1240`
   `_resolve_schemas` 合并各 middleware schema）→ 随 checkpointer 落 checkpoint。
   `permissions._auto_decision_when` 只读该字段，不自己做 IO、不调 LLM：

   - 无已存决策 / 决策为 false → True（**interrupt，fail-closed**）
   - 已存决策 true → False（免审放行）

   resume 稳定性（m3-plan 风险 3「resume 时 when 翻转」）：HITL 的 `after_model`
   在 resume 时**整节点重放**（`human_in_the_loop.py:758-870` 的 `_should_interrupt`
   会对同一 tool_call 重新求值），state 来自 interrupt 前的 checkpoint——同一
   tool_call 读到的是当初落下的同一份决策，不重新分类。探针实测（M3 Phase 3）：
   resume 时 `when` 读到的 flags 与首段完全一致。

**沙箱红线**：`SANDBOX_REDLINE_TOOLS`（execute/write_file/edit_file）在任何模式下都
不进分类器——`AUTO_ELIGIBLE` 与红线的构造期不变量在 `permissions.build_interrupt_on`；
本模块再做一道运行期过滤（工具名命中红线直接跳过，绝不给决策），单测锁定。

**MANUAL 模式**：assembly 只在 `approval_mode == "auto"` 时装配本 middleware——MANUAL
下分类器零调用、零审计行、interrupt 面与 Phase 1 逐字一致。

**fail-closed 硬要求**：分类器抛异常/超时/输出畸形/reason 为空 → 一律
`auto_approve=False`（走人工审批）并记 WARNING。绝不允许异常路径变成放行。

**审计留痕**（AC-013-3）：分类器判定 auto_approve 时，经注入的 `audit` 回调写一行
`approvals`（status=auto_approved / decided_by=classifier / decision=None /
reason=分类器理由 / risk_level 与人工审批同口径）。回调由 run_service 以**短事务**
实现（`record_auto_approval_short_tx`），middleware 不直接持有 DB 连接；落行失败只记
ERROR、不阻断工具执行（审计缺失必须可见，但工具执行不被拖死）。无审计通道（装配
缺陷）则**降级为人工审批**——没有留痕就不允许自动放行。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, NotRequired, Protocol

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.llm_factory import get_llm
from app.agent.permissions import (
    AUTO_APPROVAL_STATE_KEY,
    AUTO_ELIGIBLE,
    SANDBOX_REDLINE_TOOLS,
    RiskLevel,
    risk_level_for,
)
from app.agent.subagents import READ_ONLY_SUBAGENTS, SUBAGENT_NAMES, load_prompt
from app.core.config import agent_settings
from app.core.logger import BusinessLog

REASON_MAX_CHARS: Final[int] = 200
"""分类器理由上限（可读、可落库、不含敏感信息；契约 reason 是自由文本）。"""

DEFAULT_CLASSIFIER_TIMEOUT_S: Final[float] = 8.0
"""LLM 分类器单次决策的墙钟上限（超出即 fail-closed；配置可覆盖）。"""

ClassifierKind = Literal["rules", "llm"]


# ---------------------------------------------------------------- 决策与分类器


def _clip_reason(reason: str) -> str:
    """单行化 + 截断（reason 会进审批行与日志，不允许换行/超长）。"""
    return " ".join(str(reason or "").split())[:REASON_MAX_CHARS]


@dataclass(frozen=True, slots=True)
class AutoDecision:
    """一次分类结论：是否免审 + 可解释理由（reason 必须非空才可用于留痕）。"""

    auto_approve: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _clip_reason(self.reason))


class ApprovalClassifier(Protocol):
    """分类器接口：`auto_approve=False` 一律转人工审批（fail-closed 是默认方向）。"""

    async def classify(self, tool: str, args: Mapping[str, Any]) -> AutoDecision: ...


class RulesClassifier:
    """确定性规则分类器（默认，零网络；M3 只分类 `task`）。

    规则（按序命中，reason 说明命中哪条）：

    - R1：`task` 派发给**只读子智能体**（explorer/reviewer，工具面已剔除写类与
      execute，`subagents.READ_ONLY_EXCLUDED`）→ 免审；
    - R2：`task` 派发给预置的**受控写类子智能体**（coder/general-purpose）→ 免审，
      因为子智能体的沙箱红线工具（execute/write_file/edit_file）仍逐次人工审批；
    - R3：未知/缺失 subagent_type，或非 task 工具 → 转人工（fail-closed）。
    """

    name = "rules"

    async def classify(self, tool: str, args: Mapping[str, Any]) -> AutoDecision:
        if tool != "task":
            return AutoDecision(
                False, f"AUTO 规则 R3：工具 {tool or '<unknown>'} 不在规则白名单内，转人工审批"
            )
        raw = args.get("subagent_type")
        subagent = raw.strip() if isinstance(raw, str) else ""
        if not subagent:
            return AutoDecision(
                False, "AUTO 规则 R3：task 缺少 subagent_type，无法判定子智能体类型（fail-closed）"
            )
        if subagent not in SUBAGENT_NAMES:
            return AutoDecision(
                False,
                f"AUTO 规则 R3：子智能体 {subagent[:64]} 不在预置类型内，转人工审批（fail-closed）",
            )
        if subagent in READ_ONLY_SUBAGENTS:
            return AutoDecision(
                True,
                f"AUTO 规则 R1：子智能体 {subagent} 为只读类型（无写文件/命令执行工具），免审放行",
            )
        return AutoDecision(
            True,
            f"AUTO 规则 R2：子智能体 {subagent} 为预置受控类型；其沙箱红线工具仍需人工审批",
        )


class _ClassifierOutput(BaseModel):
    """LLM 分类器的严格输出形态（多字段/错类型/缺字段一律解析失败）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    auto_approve: bool
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)


def _text_of(message: BaseMessage) -> str:
    """取 AIMessage 的纯文本（Anthropic content 可能是 str 或 block 列表）。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(str(block) for block in blocks)
    return ""


class LlmClassifier:
    """LLM 分类器（经济档 `approval_classifier` 角色；提示词文件式）。

    输出必须严格解析为 `{"auto_approve": bool, "reason": str}`；解析失败/超时/调用
    异常/空理由**一律** fail-closed 并记 WARNING。`model_factory` 可在测试注入假模型。
    """

    name = "llm"

    def __init__(
        self,
        model_factory: Callable[[], BaseChatModel] | None = None,
        *,
        timeout_s: float | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._model_factory = model_factory
        self._timeout_s = DEFAULT_CLASSIFIER_TIMEOUT_S if timeout_s is None else timeout_s
        self._system_prompt = system_prompt

    def _model(self) -> BaseChatModel:
        if self._model_factory is not None:
            return self._model_factory()
        return get_llm("approval_classifier")

    async def classify(self, tool: str, args: Mapping[str, Any]) -> AutoDecision:
        payload = json.dumps(
            {"tool": tool, "args": dict(args)}, ensure_ascii=False, sort_keys=True, default=str
        )
        messages = [
            SystemMessage(content=self._system_prompt or load_prompt("approval_classifier")),
            HumanMessage(content=payload),
        ]
        try:
            response = await asyncio.wait_for(
                self._model().ainvoke(messages), timeout=self._timeout_s
            )
        except TimeoutError:
            _classifier_warning(tool, "分类器超时，转人工审批（fail-closed）")
            return AutoDecision(False, "分类器超时，转人工审批（fail-closed）")
        except Exception as exc:  # noqa: BLE001 —— 分类器故障绝不允许变成放行
            _classifier_warning(tool, "分类器调用失败，转人工审批（fail-closed）", exc)
            return AutoDecision(False, "分类器调用失败，转人工审批（fail-closed）")
        return self._parse(tool, response)

    def _parse(self, tool: str, response: BaseMessage) -> AutoDecision:
        try:
            raw = json.loads(_text_of(response))
            parsed = _ClassifierOutput.model_validate(raw)
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            _classifier_warning(tool, "分类器输出无法解析，转人工审批（fail-closed）", exc)
            return AutoDecision(False, "分类器输出无法解析，转人工审批（fail-closed）")
        reason = _clip_reason(parsed.reason)
        if not reason:
            _classifier_warning(tool, "分类器理由为空，转人工审批（fail-closed）")
            return AutoDecision(False, "分类器理由为空，转人工审批（fail-closed）")
        return AutoDecision(parsed.auto_approve, reason)


def _classifier_warning(tool: str, message: str, exc: Exception | None = None) -> None:
    """分类器故障留痕（WARNING；原始异常栈只在日志里，reason 回流给模型面的是固定文案）。"""
    BusinessLog(
        message=message,
        action="分类审批",
        entity="approval",
        entity_id=tool,
    ).warning(exc)


def build_classifier(kind: ClassifierKind | None = None) -> ApprovalClassifier:
    """按配置构造分类器（默认 rules：测试与无凭证环境零网络）。"""
    resolved = kind or agent_settings.auto_approval_classifier
    if resolved == "llm":
        return LlmClassifier(timeout_s=agent_settings.auto_approval_timeout_s)
    return RulesClassifier()


# ---------------------------------------------------------------- 审计载荷


@dataclass(frozen=True, slots=True)
class AutoApprovalRecord:
    """一条待落库的 AUTO 决策（middleware → service 的**进程内**内部载荷，不上 wire）。"""

    tool_call_id: str
    tool: str
    args: dict[str, Any]
    risk_level: RiskLevel
    reason: str


AutoApprovalAuditSink = Callable[[Sequence[AutoApprovalRecord]], Awaitable[None]]
"""审计落行回调（run_service 以短事务实现；不得新增 wire 事件）。"""


@dataclass(frozen=True, slots=True)
class AutoApprovalRuntime:
    """一次 run 的分类器运行上下文（每次 run 随内层 agent 重建）。"""

    session_id: str
    run_id: str
    user_id: str = "local"
    audit: AutoApprovalAuditSink | None = None


# ---------------------------------------------------------------- 状态载体


class AutoApprovalState(AgentState[Any]):
    """middleware 的 state 扩展：按 tool_call_id 索引的分类决策（随 checkpoint 存活）。

    `PrivateStateAttr` 只把字段从 input/output schema 里隐藏（`types.py:345`），
    不影响 state channel 与 checkpoint 持久化——这正是 `when` 谓词在 resume 段
    读到同一份决策的前提。
    """

    auto_approval_decisions: NotRequired[Annotated[dict[str, dict[str, Any]], PrivateStateAttr]]


# ---------------------------------------------------------------- middleware


@dataclass(slots=True)
class _DecisionBook:
    """本段已落审计的 tool_call_id（resume 重放不重复落行；单事件循环内无竞态）。"""

    audited: set[str] = field(default_factory=set)


class AutoApprovalMiddleware(AgentMiddleware):
    """模型产出工具调用后分类 AUTO_ELIGIBLE 工具，决策写入 state 供 HITL `when` 读取。

    只实现 `awrap_model_call`：M2 起外层图恒以 async 跑（`RunExecutor.astream`），同步
    执行路径不经过本 middleware（与 `LCodeEventTapMiddleware` 同口径）。

    时序取证（安装版 langchain 1.4.6）：`awrap_model_call` 在 model node 内执行，
    返回的 `ExtendedModelResponse.command` 与模型消息同批写成 state 更新
    （`factory.py:1601-1607` / `_build_commands`），随后才是 `after_model` 链；
    HITL 的 `after_model` 在链首（deepagents 栈尾），因此能读到本 middleware 落下的
    决策——探针实测（M3 Phase 3 spike）。
    """

    name = "LCodeAutoApprovalMiddleware"
    state_schema = AutoApprovalState

    def __init__(
        self,
        *,
        classifier: ApprovalClassifier,
        runtime: AutoApprovalRuntime,
    ) -> None:
        self._classifier = classifier
        self._runtime = runtime
        self._book = _DecisionBook()

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        response = await handler(request)
        decisions = await self._classify_calls(response)
        # 每轮覆盖（含空表）：上一轮的决策绝不允许借 tool_call_id 复用到新调用上。
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={AUTO_APPROVAL_STATE_KEY: decisions}),
        )

    async def _classify_calls(self, response: ModelResponse[Any]) -> dict[str, dict[str, Any]]:
        decisions: dict[str, dict[str, Any]] = {}
        for message in response.result:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                decision = await self._decide_one(call)
                if decision is not None:
                    tool_call_id, payload = decision
                    decisions[tool_call_id] = payload
        return decisions

    async def _decide_one(self, call: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """单个工具调用 → (tool_call_id, state 载荷)；不参与分类的调用返回 None。"""
        tool = str(call.get("name") or "")
        tool_call_id = str(call.get("id") or "")
        if not tool_call_id or tool not in AUTO_ELIGIBLE:
            return None
        if tool in SANDBOX_REDLINE_TOOLS:  # 运行期防线（构造期不变量之外的第二道）
            return None

        raw_args = call.get("args")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        decision = await self._classify_safely(tool, args)
        if decision.auto_approve and not await self._audit(
            tool_call_id, tool, args, decision.reason
        ):
            # 审计通道缺失 = 装配缺陷（不是落行失败）：没有留痕就不自动放行。
            decision = AutoDecision(False, "AUTO 审计通道不可用，降级人工审批（fail-closed）")
        return tool_call_id, {
            "auto_approve": decision.auto_approve,
            "reason": decision.reason,
            "tool": tool,
        }

    async def _classify_safely(self, tool: str, args: Mapping[str, Any]) -> AutoDecision:
        try:
            decision = await self._classifier.classify(tool, args)
        except Exception as exc:  # noqa: BLE001 —— 分类器自身故障也必须 fail-closed
            _classifier_warning(tool, "分类器异常，转人工审批（fail-closed）", exc)
            return AutoDecision(False, "分类器异常，转人工审批（fail-closed）")
        if decision.auto_approve and not decision.reason:
            # 空理由无法留痕（AC-013-3 的三要素缺一）：不允许放行（自定义/未来分类器兜底）。
            _classifier_warning(tool, "分类器理由为空，转人工审批（fail-closed）")
            return AutoDecision(False, "分类器理由为空，转人工审批（fail-closed）")
        return decision

    async def _audit(
        self,
        tool_call_id: str,
        tool: str,
        args: Mapping[str, Any],
        reason: str,
    ) -> bool:
        """写审计行（幂等 + 失败不阻断）；返回是否具备审计能力。"""
        if self._runtime.audit is None:
            BusinessLog(
                message="AUTO 分类器已判定免审，但未注入审计通道（降级人工审批）",
                action="落库审批",
                entity="approval",
                entity_id=tool_call_id,
            ).error()
            return False
        if tool_call_id in self._book.audited:
            return True
        self._book.audited.add(tool_call_id)
        risk_level: RiskLevel = risk_level_for(tool, args) or "medium"
        record = AutoApprovalRecord(
            tool_call_id=tool_call_id,
            tool=tool,
            args=dict(args),
            risk_level=risk_level,
            reason=reason,
        )
        try:
            await self._runtime.audit([record])
        except Exception as exc:  # noqa: BLE001 —— 审计缺失严重但不得阻断工具执行（ERROR 留痕）
            self._book.audited.discard(tool_call_id)  # 允许后续重放重试（DB 侧按 id 幂等）
            BusinessLog(
                message="AUTO 审批审计行落库失败（决策仍生效，审计缺失需人工排查）",
                action="落库审批",
                entity="approval",
                entity_id=tool_call_id,
            ).error(exc)
        return True


def build_auto_approval_middleware(
    runtime: AutoApprovalRuntime | None,
) -> AutoApprovalMiddleware:
    """装配 AUTO 分类 middleware（assembly 只在 approval_mode=auto 时调用）。

    `runtime=None` 是装配缺陷（run 路径恒注入）：此时保留分类器行为，但审计通道为
    None —— 任何 auto_approve 决策都会降级人工审批（fail-closed）。
    """
    resolved = runtime or AutoApprovalRuntime(session_id="", run_id="")
    return AutoApprovalMiddleware(classifier=build_classifier(), runtime=resolved)


__all__ = [
    "DEFAULT_CLASSIFIER_TIMEOUT_S",
    "REASON_MAX_CHARS",
    "ApprovalClassifier",
    "AutoApprovalAuditSink",
    "AutoApprovalMiddleware",
    "AutoApprovalRecord",
    "AutoApprovalRuntime",
    "AutoApprovalState",
    "AutoDecision",
    "ClassifierKind",
    "LlmClassifier",
    "RulesClassifier",
    "build_auto_approval_middleware",
    "build_classifier",
]
