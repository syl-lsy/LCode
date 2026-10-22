"""postgres_checkpointer 单测（安全审计 P3-2）：e2e 强制口的运行环境判别。

`LCODE_E2E_CHECKPOINTER` 是**测试车道**的强制口：`ENVIRONMENT=production` 下必须被
忽略——否则一个测试专用环境变量就能把真库 saver 静默降级为 InMemorySaver（进程重启后
审批断点/resume 播种全丢）。本文件只测模式解析（`_resolve_mode`）：不建连接、不碰库。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from app.core import postgres_checkpointer as module


def _settings(*, environment: str = "development", checkpointer: str = "postgres") -> Any:
    """配置组替身：只撑起 `_resolve_mode` 读取的两个字段（两组共用同一形态）。"""
    return SimpleNamespace(environment=environment, checkpointer=checkpointer)


def test_production_ignores_e2e_checkpointer_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """production + 强制口=memory → 仍为 postgres（并在日志里留下忽略痕迹）。"""
    monkeypatch.setenv(module.E2E_CHECKPOINTER_ENV, "memory")
    monkeypatch.setattr(module, "common_settings", _settings(environment="production"))
    monkeypatch.setattr(module, "agent_settings", _settings(environment="production"))

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        mode = module._resolve_mode()
    finally:
        logger.remove(sink_id)

    assert mode == "postgres"
    warnings = [
        record
        for record in captured
        if record["level"].name == "WARNING" and record["extra"].get("entity") == "config"
    ]
    assert warnings, "production 忽略强制口必须留 WARNING（可审计）"


def test_e2e_checkpointer_override_applies_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 production + 强制口=memory → memory（e2e 车道语义不变）。"""
    monkeypatch.setenv(module.E2E_CHECKPOINTER_ENV, "memory")
    monkeypatch.setattr(module, "common_settings", _settings(environment="development"))
    monkeypatch.setattr(module, "agent_settings", _settings(environment="development"))

    assert module._resolve_mode() == "memory"


def test_e2e_checkpointer_override_wins_over_config_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 production 下强制口仍覆盖配置值（postgres ← memory 配置）。"""
    monkeypatch.setenv(module.E2E_CHECKPOINTER_ENV, "postgres")
    monkeypatch.setattr(module, "common_settings", _settings(environment="test"))
    monkeypatch.setattr(module, "agent_settings", _settings(checkpointer="memory"))

    assert module._resolve_mode() == "postgres"


def test_unset_override_uses_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(module.E2E_CHECKPOINTER_ENV, raising=False)
    monkeypatch.setattr(module, "common_settings", _settings(environment="development"))
    monkeypatch.setattr(module, "agent_settings", _settings(checkpointer="memory"))

    assert module._resolve_mode() == "memory"


def test_invalid_override_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法值 fail loudly（不静默回落 postgres）。"""
    monkeypatch.setenv(module.E2E_CHECKPOINTER_ENV, "sqlite")
    monkeypatch.setattr(module, "common_settings", _settings(environment="development"))
    monkeypatch.setattr(module, "agent_settings", _settings(checkpointer="postgres"))

    with pytest.raises(ValueError, match="checkpointer 后端非法"):
        module._resolve_mode()
