"""AUTO 审批分类器单测（M3 Phase 3；REQ-013 AC-013-3 / AC-013-4）。

锁定的行为面：

- 规则分类器：只读子智能体（R1）/ 受控写类子智能体（R2）免审；未知或缺失
  subagent_type、非 task 工具一律转人工（R3，fail-closed）；reason 可解释；
- LLM 分类器：严格 JSON 解析；畸形/多余字段/错类型/空 reason/异常/超时全部
  fail-closed（异常路径绝不允许变成放行）；
- middleware：决策按 tool_call_id 写入 state command（HITL `when` 的输入）、每次
  模型回合覆盖旧决策、审计行按 tool_call_id 幂等、审计缺失降级人工、落行失败不阻断；
- `when` 谓词三态（无决策/false → interrupt；true → 免审）与 resume 稳定性（已存
  false 时即使分类器此刻会放行也不放行，且谓词不求值分类器）；
- 沙箱红线工具永不进分类器（运行期第二道防线）。
"""

import asyncio
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from app.agent import auto_approval as auto
from app.agent.auto_approval import (
    AutoApprovalMiddleware,
    AutoApprovalRecord,
    AutoApprovalRuntime,
    AutoDecision,
    LlmClassifier,
    RulesClassifier,
    build_classifier,
)
from app.agent.permissions import AUTO_APPROVAL_STATE_KEY, AUTO_ELIGIBLE, _auto_decision_when

# ---------------------------------------------------------------- 规则分类器


def _task(subagent: str) -> dict[str, Any]:
    return {"description": "do it", "subagent_type": subagent}


@pytest.mark.parametrize("subagent", ["explorer", "reviewer"])
async def test_rules_approves_read_only_subagents(subagent: str) -> None:
    """R1：只读子智能体（无写文件/execute 工具）→ 免审，reason 说明命中规则。"""
    decision = await RulesClassifier().classify("task", _task(subagent))

    assert decision.auto_approve is True
    assert "R1" in decision.reason and subagent in decision.reason


@pytest.mark.parametrize("subagent", ["coder", "general-purpose"])
async def test_rules_approves_controlled_write_subagents(subagent: str) -> None:
    """R2：预置受控类型 → 免审（其 execute/write 仍逐次人工审批）。"""
    decision = await RulesClassifier().classify("task", _task(subagent))

    assert decision.auto_approve is True
    assert "R2" in decision.reason
    assert "红线" in decision.reason


@pytest.mark.parametrize(
    "args",
    [
        {"subagent_type": "unknown-agent"},
        {"subagent_type": ""},
        {"description": "缺 subagent_type"},
        {"subagent_type": 123},
    ],
)
async def test_rules_denies_unknown_or_missing_subagent(args: dict[str, Any]) -> None:
    """R3：未知/缺失 subagent_type → 转人工（fail-closed）。"""
    decision = await RulesClassifier().classify("task", args)

    assert decision.auto_approve is False
    assert "R3" in decision.reason


async def test_rules_denies_non_task_tools() -> None:
    """M3 只分类 task；其它工具一律 fail-closed（M4 扩集合时再显式加规则）。"""
    decision = await RulesClassifier().classify("execute", {"command": "ls"})

    assert decision.auto_approve is False
    assert "R3" in decision.reason


def test_auto_decision_clips_and_single_lines_reason() -> None:
    """reason 单行化 + 截断（可落库、可展示；不把大段/多行文本带进审批行）。"""
    decision = AutoDecision(True, "  line1\nline2\t" + "x" * 500)

    assert "\n" not in decision.reason and "\t" not in decision.reason
    assert len(decision.reason) == auto.REASON_MAX_CHARS
    assert decision.reason.startswith("line1 line2")


def test_build_classifier_defaults_to_rules() -> None:
    assert isinstance(build_classifier(), RulesClassifier)
    assert isinstance(build_classifier("llm"), LlmClassifier)


# ---------------------------------------------------------------- LLM 分类器（fail-closed）


class _FakeModel:
    """分类器模型的替身：成功/抛错/延时三态（`LlmClassifier` 只消费 ainvoke）。"""

    def __init__(
        self,
        *,
        content: str | None = None,
        exc: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._exc = exc
        self._delay = delay
        self.calls: list[list[BaseMessage]] = []

    async def ainvoke(self, messages: list[BaseMessage], **kwargs: Any) -> AIMessage:
        self.calls.append(list(messages))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return AIMessage(content=self._content or "")


def _llm_classifier(
    content: str | None = None,
    *,
    exc: Exception | None = None,
    delay: float = 0.0,
    timeout_s: float = 1.0,
) -> tuple[LlmClassifier, _FakeModel]:
    model = _FakeModel(content=content, exc=exc, delay=delay)
    classifier = LlmClassifier(
        lambda: cast(BaseChatModel, model), timeout_s=timeout_s, system_prompt="classify"
    )
    return classifier, model


async def test_llm_classifier_parses_strict_json() -> None:
    classifier, _ = _llm_classifier('{"auto_approve": true, "reason": "命中规则 1"}')

    decision = await classifier.classify("task", _task("explorer"))

    assert decision == AutoDecision(True, "命中规则 1")


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"auto_approve": true, "reason": "x"}\n```',  # Markdown 包裹
        "not json",
        '{"auto_approve": true}',  # 缺 reason
        '{"auto_approve": true, "reason": "x", "extra": 1}',  # 多余字段
        '{"auto_approve": "true", "reason": "x"}',  # 字符串布尔（strict）
        '{"auto_approve": true, "reason": "   "}',  # 空理由
        "",  # 空输出
    ],
)
async def test_llm_classifier_malformed_output_fails_closed(content: str) -> None:
    """畸形输出：解析失败 → auto_approve=False（绝不因解析宽松而放行）。"""
    classifier, _ = _llm_classifier(content)

    decision = await classifier.classify("task", _task("coder"))

    assert decision.auto_approve is False
    assert "fail-closed" in decision.reason


async def test_llm_classifier_exception_fails_closed() -> None:
    classifier, _ = _llm_classifier(exc=RuntimeError("gateway down"))

    decision = await classifier.classify("task", _task("coder"))

    assert decision.auto_approve is False
    assert "失败" in decision.reason


async def test_llm_classifier_timeout_fails_closed() -> None:
    classifier, _ = _llm_classifier(
        '{"auto_approve": true, "reason": "x"}', delay=0.05, timeout_s=0.005
    )

    decision = await classifier.classify("task", _task("coder"))

    assert decision.auto_approve is False
    assert "超时" in decision.reason


# ---------------------------------------------------------------- middleware


class _SpyClassifier:
    def __init__(self, decision: AutoDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def classify(self, tool: str, args: Any) -> AutoDecision:
        self.calls.append((tool, dict(args)))
        return self.decision


class _AuditSpy:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[AutoApprovalRecord] = []

    async def __call__(self, records: Any) -> None:
        self.records.extend(records)
        if self.fail:
            raise RuntimeError("audit db down")


def _tool_call(call_id: str, name: str = "task", args: dict[str, Any] | None = None) -> dict:
    return {
        "name": name,
        "args": args or _task("coder"),
        "id": call_id,
        "type": "tool_call",
    }


async def _invoke(
    middleware: AutoApprovalMiddleware, *tool_calls: dict
) -> dict[str, dict[str, Any]]:
    response = ModelResponse(
        result=[AIMessage(content="", tool_calls=list(tool_calls))], structured_response=None
    )

    async def handler(request: ModelRequest[Any]) -> ModelResponse[Any]:
        return response

    result = await middleware.awrap_model_call(cast(ModelRequest[Any], object()), handler)
    command = result.command  # type: ignore[union-attr]  # 恒为 ExtendedModelResponse
    assert command is not None
    update = cast(dict[str, Any], command.update)
    return cast(dict[str, dict[str, Any]], update[AUTO_APPROVAL_STATE_KEY])


def _runtime(audit: _AuditSpy | None) -> AutoApprovalRuntime:
    return AutoApprovalRuntime(session_id="s", run_id="r", audit=audit)


async def test_middleware_writes_decision_and_audit_record() -> None:
    """auto_approve 决策：state 载荷 + 一条审计载荷（risk/reason 与人工审批同口径）。"""
    audit = _AuditSpy()
    middleware = AutoApprovalMiddleware(classifier=RulesClassifier(), runtime=_runtime(audit))

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is True
    assert "R2" in decisions["call-1"]["reason"]
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record.tool_call_id == "call-1"
    assert record.tool == "task"
    assert record.risk_level == "medium"
    assert "R2" in record.reason


async def test_middleware_denied_decision_writes_state_but_no_audit() -> None:
    """分类器拒绝 → state 记 false（resume 稳定）但不落 auto_approved 审计行。"""
    audit = _AuditSpy()
    classifier = _SpyClassifier(AutoDecision(False, "拒绝"))
    middleware = AutoApprovalMiddleware(classifier=classifier, runtime=_runtime(audit))

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is False
    assert audit.records == []


async def test_middleware_dedupes_audit_by_tool_call_id() -> None:
    """resume 重放同一 tool_call 不重复落审计（进程内幂等）。"""
    audit = _AuditSpy()
    middleware = AutoApprovalMiddleware(classifier=RulesClassifier(), runtime=_runtime(audit))

    await _invoke(middleware, _tool_call("call-1"))
    await _invoke(middleware, _tool_call("call-1"))

    assert len(audit.records) == 1


async def test_middleware_clears_stale_decisions_each_turn() -> None:
    """每轮覆盖决策表：上一轮的 true 绝不允许借同一 tool_call_id 复用到新调用。"""
    audit = _AuditSpy()
    middleware = AutoApprovalMiddleware(classifier=RulesClassifier(), runtime=_runtime(audit))

    first = await _invoke(middleware, _tool_call("call-1"))
    assert first["call-1"]["auto_approve"] is True
    # 第二轮没有工具调用：字段必须被清空（不是保留上一轮）
    cleared = await _invoke(middleware)
    assert cleared == {}


async def test_middleware_missing_audit_channel_downgrades_to_manual() -> None:
    """装配缺陷（无审计通道）：决策降级 false——没有留痕就不允许自动放行。"""
    middleware = AutoApprovalMiddleware(classifier=RulesClassifier(), runtime=_runtime(None))

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is False
    assert "审计" in decisions["call-1"]["reason"]


async def test_middleware_audit_failure_does_not_block_execution() -> None:
    """落行失败：决策仍为 true（工具继续执行），失败只留 ERROR。"""
    audit = _AuditSpy(fail=True)
    middleware = AutoApprovalMiddleware(classifier=RulesClassifier(), runtime=_runtime(audit))

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is True
    assert len(audit.records) == 1


class _BoomClassifier:
    async def classify(self, tool: str, args: Any) -> AutoDecision:
        raise RuntimeError("classifier exploded")


async def test_classifier_exception_fails_closed_without_audit() -> None:
    """分类器自身抛错：fail-closed + 不落审计（异常绝不允许变成放行）。"""
    audit = _AuditSpy()
    middleware = AutoApprovalMiddleware(classifier=_BoomClassifier(), runtime=_runtime(audit))

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is False
    assert audit.records == []


async def test_classifier_empty_reason_fails_closed() -> None:
    """空理由：无法留痕 → 不允许放行（自定义分类器的兜底，AC-013-3 三要素齐备）。"""
    audit = _AuditSpy()
    middleware = AutoApprovalMiddleware(
        classifier=_SpyClassifier(AutoDecision(True, "   ")), runtime=_runtime(audit)
    )

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is False
    assert "理由为空" in decisions["call-1"]["reason"]
    assert audit.records == []


async def test_red_line_tool_is_never_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """沙箱红线永不进分类器（即使 AUTO_ELIGIBLE 被误改，运行期也跳过）。"""
    monkeypatch.setattr(auto, "AUTO_ELIGIBLE", AUTO_ELIGIBLE | {"execute"})
    audit = _AuditSpy()
    classifier = _SpyClassifier(AutoDecision(True, "should not be used"))
    middleware = AutoApprovalMiddleware(classifier=classifier, runtime=_runtime(audit))

    decisions = await _invoke(
        middleware, _tool_call("call-exec", name="execute", args={"command": "rm -rf /"})
    )

    assert decisions == {}
    assert classifier.calls == []
    assert audit.records == []


def _request(state: Any = None, *, call_id: str = "call-1") -> ToolCallRequest:
    payload: dict[str, Any] = {"tool_call": {"id": call_id, "name": "task", "args": _task("coder")}}
    if state is not None:
        payload["state"] = state
    return cast(ToolCallRequest, type("_Req", (), payload)())


def test_when_predicate_is_tri_state() -> None:
    """无决策/无 state → True；决策 false → True；决策 true → False（fail-closed）。"""
    assert _auto_decision_when(_request()) is True
    assert _auto_decision_when(_request({})) is True
    assert (
        _auto_decision_when(
            _request({AUTO_APPROVAL_STATE_KEY: {"call-1": {"auto_approve": False}}})
        )
        is True
    )
    assert (
        _auto_decision_when(_request({AUTO_APPROVAL_STATE_KEY: {"call-1": {"auto_approve": True}}}))
        is False
    )
    # 畸形决策（非 dict / 非 bool）按「无决策」处理 → interrupt
    assert _auto_decision_when(_request({AUTO_APPROVAL_STATE_KEY: {"call-1": "x"}})) is True
    assert (
        _auto_decision_when(
            _request({AUTO_APPROVAL_STATE_KEY: {"call-1": {"auto_approve": "yes"}}})
        )
        is True
    )


async def test_stored_denial_survives_even_if_classifier_would_now_approve() -> None:
    """resume 稳定性（m3-plan 风险 3）：`when` 只读已存决策，不重新分类。

    首轮分类器拒绝 → state 记 false；随后换一个「此刻会放行」的分类器求值同一 state：
    谓词仍是 True（interrupt），且新分类器零调用。
    """
    audit = _AuditSpy()
    first = AutoApprovalMiddleware(
        classifier=_SpyClassifier(AutoDecision(False, "首轮拒绝")), runtime=_runtime(audit)
    )
    decisions = await _invoke(first, _tool_call("call-1"))
    assert decisions["call-1"]["auto_approve"] is False

    later = _SpyClassifier(AutoDecision(True, "此刻会放行"))
    request = _request({AUTO_APPROVAL_STATE_KEY: decisions})
    assert _auto_decision_when(request) is True
    assert later.calls == [], "when 谓词不得求值分类器（否则 resume 可能翻转）"


def test_state_schema_declares_the_decision_key() -> None:
    """state 字段名与 `AUTO_APPROVAL_STATE_KEY` 必须一致（否则 command 更新会被丢弃）。"""
    assert AUTO_APPROVAL_STATE_KEY in auto.AutoApprovalState.__annotations__


async def test_build_middleware_without_runtime_downgrades_to_manual() -> None:
    """装配缺陷（runtime=None）：auto_approve 决策降级人工审批（fail-closed）。"""
    middleware = auto.build_auto_approval_middleware(None)

    decisions = await _invoke(middleware, _tool_call("call-1"))

    assert decisions["call-1"]["auto_approve"] is False
    assert "审计" in decisions["call-1"]["reason"]
