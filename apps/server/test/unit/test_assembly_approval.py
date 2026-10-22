"""装配单测（m3-plan Phase 1 A / m3-spikes §1.2）：approval_mode 透传与 HITL 装配形态。

装配口径（以安装版 deepagents 0.6.12 为准）：`interrupt_on=` 交给 create_deep_agent，
主图 tail 自动装 HumanInTheLoopMiddleware，声明式子智能体继承同一策略；**不**手装 HITL、
**不**传 checkpointer（父图已挂，传了会多一层嵌套 saver）。

本文件用 create_deep_agent 替身锁装配入参（真图行为由 integration 审批流用例覆盖）。
"""

from typing import Any, cast

import pytest
from langgraph.graph.state import CompiledStateGraph

from app.agent import assembly
from app.agent.middleware import LCodeEventTapMiddleware
from app.agent.models_registry import SessionModelCfg
from test.support import scripts
from test.support.fake_llm import ScriptedChatModel


def test_default_model_factory_normalizes_state_snapshot_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认工厂必须把 state 快照 dict 归一化成 SessionModelCfg（真模型路径回归）。

    `RunExecutor.run` 把 dict 快照交给 `default_model_factory`；`models_registry.resolve`
    按属性取值，旧实现原样透传 dict 会在真实模型路径 `'dict' object has no attribute
    'model'` 崩溃（假模型车道覆盖了工厂接缝，M3 Phase 6 真机取证发现）。
    """
    captured: dict[str, Any] = {}

    def fake_get_llm(role: str, *, session_cfg: SessionModelCfg) -> Any:
        captured["role"] = role
        captured["session_cfg"] = session_cfg
        return cast(Any, object())

    monkeypatch.setattr(assembly, "get_llm", fake_get_llm)
    factory = assembly.default_model_factory(
        {
            "model": "qwen3.7-plus",
            "reasoning_effort": "high",
            "approval_mode": "manual",
            "sandbox_provider": "bailian",
        }
    )
    factory("main")

    assert captured["role"] == "main"
    resolved = captured["session_cfg"]
    assert isinstance(resolved, SessionModelCfg)
    assert (resolved.model, resolved.reasoning_effort) == ("qwen3.7-plus", "high")


def _model_factory(role: str) -> ScriptedChatModel:
    model = ScriptedChatModel(steps=scripts.simple_reply())
    model.role = role
    return model


def _build(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    """调 build_inner_agent，捕获 create_deep_agent 的调用 kwargs。"""
    captured: dict[str, Any] = {}

    def fake_create_deep_agent(*args: Any, **call_kwargs: Any) -> Any:
        captured["args"] = args
        captured.update(call_kwargs)
        return cast(CompiledStateGraph, object())

    monkeypatch.setattr(assembly, "create_deep_agent", fake_create_deep_agent)
    assembly.build_inner_agent(
        None,
        cast(Any, None),  # backend 只被透传（替身不消费）
        model_factory=_model_factory,
        **kwargs,
    )
    return captured


def test_default_mode_is_manual_and_passes_full_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 MANUAL：interrupt_on 含 fs 写类 + execute + task（full gate）。"""
    captured = _build(monkeypatch)
    interrupt_on = captured["interrupt_on"]
    assert set(interrupt_on) == {"write_file", "edit_file", "execute", "task"}


def test_auto_mode_keeps_task_behind_classifier_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTO：task 仍在 interrupt 面（谓词读分类器决策）；沙箱红线仍在面内（AC-013-4）。"""
    captured = _build(monkeypatch, approval_mode="auto")
    interrupt_on = captured["interrupt_on"]
    assert "task" in interrupt_on, "AUTO 的 task 必须留在审批面（分类器决策 + 审计）"
    assert {"execute", "write_file", "edit_file"} <= set(interrupt_on)
    names = [middleware.name for middleware in captured["middleware"]]
    assert "LCodeAutoApprovalMiddleware" in names, "AUTO 必须装配分类器 middleware"


def test_manual_mode_does_not_install_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """MANUAL：分类器零装配（零调用、零审计行；行为与 Phase 1 逐字一致）。"""
    captured = _build(monkeypatch)
    names = [middleware.name for middleware in captured["middleware"]]
    assert "LCodeAutoApprovalMiddleware" not in names


def test_checkpointer_is_not_passed_to_inner_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkpointer 只挂父图（m3-spikes §1.2）：内层装配不得再传 saver。"""
    captured = _build(monkeypatch)
    assert "checkpointer" not in captured


def test_user_middleware_order_is_retry_then_event_tap(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户 middleware 顺序：retry 在内、event tap 在外（A3 §5.2；名单制）。"""
    captured = _build(monkeypatch)
    names = [middleware.name for middleware in captured["middleware"]]
    assert names == ["ToolRetryMiddleware", "LCodeEventTapMiddleware"]
    assert any(isinstance(m, LCodeEventTapMiddleware) for m in captured["middleware"])


def test_auto_middleware_is_appended_after_event_tap(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTO 分类器在名单末尾（只在 model-call 钩子工作，不扰动工具 tap/重试序）。"""
    captured = _build(monkeypatch, approval_mode="auto")
    names = [middleware.name for middleware in captured["middleware"]]
    assert names == [
        "ToolRetryMiddleware",
        "LCodeEventTapMiddleware",
        "LCodeAutoApprovalMiddleware",
    ]


def test_unknown_mode_is_rejected_before_graph_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 approval_mode 在装配期就失败（build_interrupt_on 抛 ValueError）。"""
    with pytest.raises(ValueError, match="未知审批模式"):
        _build(monkeypatch, approval_mode="always")
