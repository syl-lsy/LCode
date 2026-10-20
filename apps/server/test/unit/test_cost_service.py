"""unit 层：成本估算口径（REQ-015 / AC-015-1、AC-015-4；test-plan-m2 §2.6）。

本文件只测 cost_service 的**纯函数面**（估算/定价/聚合/落行口径）——不连库（三维汇总的
SQL 侧在 integration/test_rest_api.py 覆盖）。两个契约锚：

- AC-015-4：running 单价 SANDBOX_COST_RATE_PER_HOUR（1C2G ≈ 0.156 元/h）、paused 保留价
  SANDBOX_PAUSED_RATE_PER_GIB_HOUR（元/GiB·h，P0 规格 2GiB）；
- AC-015-1：思考 token 计入 output（PRD 硬约束 9）——落行时不二次叠加。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import sandbox_settings
from app.sandbox.base import SandboxUsageSegment
from app.services import cost_service
from app.services.cost_service import (
    DEFAULT_SANDBOX_GIB,
    ModelCallEntry,
    aggregate_costs,
    build_model_call,
    estimate_sandbox_cost,
    model_cost_of,
)

RUNNING_RATE = 0.156
PAUSED_RATE = 0.0021

BASE = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def _segment(
    phase: str,
    *,
    duration_seconds: int | None = None,
    started_at: datetime | None = None,
) -> SandboxUsageSegment:
    started = (
        started_at if started_at is not None else BASE - timedelta(seconds=duration_seconds or 0)
    )
    return SandboxUsageSegment(
        phase=phase,  # type: ignore[arg-type]  # 测试入参恒为 running/paused 字面量
        started_at=started,
        ended_at=BASE if duration_seconds is not None else None,
        duration_seconds=duration_seconds,
    )


# ---------------------------------------------------------------- 沙箱费用估算（AC-015-4）


def test_estimate_running_and_paused_rates() -> None:
    """closed 段：running 1h × 运行价 + paused 0.5h × 保留价 × 2GiB。"""
    estimate = estimate_sandbox_cost(
        [_segment("running", duration_seconds=3600), _segment("paused", duration_seconds=1800)],
        rate_per_hour=RUNNING_RATE,
        paused_rate_per_gib_hour=PAUSED_RATE,
        now=BASE,
    )

    assert estimate.rate_per_hour == pytest.approx(RUNNING_RATE)
    assert estimate.currency == "CNY"
    assert estimate.accrued == pytest.approx(RUNNING_RATE + PAUSED_RATE * DEFAULT_SANDBOX_GIB * 0.5)


def test_estimate_accrues_open_running_segment_up_to_now() -> None:
    """进行中的 running 段按时长（now - started_at）计入，不要求先封口。"""
    estimate = estimate_sandbox_cost(
        [],
        open_segment=_segment("running", started_at=BASE - timedelta(minutes=30)),
        rate_per_hour=RUNNING_RATE,
        paused_rate_per_gib_hour=PAUSED_RATE,
        now=BASE,
    )

    assert estimate.accrued == pytest.approx(RUNNING_RATE * 0.5)


def test_estimate_no_segments_is_zero() -> None:
    """无分段（会话从未创建沙箱）：accrued=0、rate 仍如实报运行单价。"""
    estimate = estimate_sandbox_cost(
        [], rate_per_hour=RUNNING_RATE, paused_rate_per_gib_hour=PAUSED_RATE, now=BASE
    )

    assert estimate.accrued == 0.0
    assert estimate.rate_per_hour == pytest.approx(RUNNING_RATE)


def test_estimate_defaults_to_configured_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省费率取 SANDBOX_ 配置组（配置即口径的单一来源）。"""
    monkeypatch.setattr(sandbox_settings, "cost_rate_per_hour", 0.3)
    monkeypatch.setattr(sandbox_settings, "paused_rate_per_gib_hour", 0.004)

    estimate = estimate_sandbox_cost(
        [_segment("running", duration_seconds=3600), _segment("paused", duration_seconds=1800)],
        now=BASE,
    )

    assert estimate.rate_per_hour == pytest.approx(0.3)
    assert estimate.accrued == pytest.approx(0.3 + 0.004 * DEFAULT_SANDBOX_GIB * 0.5)


# ---------------------------------------------------------------- 模型成本口径（AC-015-1）


def test_model_cost_from_per_mtok_pricing() -> None:
    """元 = token/1e6 × 每百万单价；分项独立计价后求和。"""
    cost = model_cost_of(
        input_tokens=1_000_000, output_tokens=500_000, input_per_mtok=2.0, output_per_mtok=8.0
    )

    assert cost == pytest.approx(2.0 + 4.0)


def test_model_cost_zero_tokens_is_zero() -> None:
    assert (
        model_cost_of(input_tokens=0, output_tokens=0, input_per_mtok=2.0, output_per_mtok=8.0)
        == 0.0
    )


def test_build_model_call_keeps_thinking_tokens_in_output() -> None:
    """思考 token 已含在 output（网关口径）——落行原样保留，不二次叠加。"""
    session_id = uuid4()
    run_id = uuid4()

    row = build_model_call(
        ModelCallEntry(
            session_id=str(session_id),
            run_id=str(run_id),
            model="qwen3.7-plus",
            input_tokens=12,
            output_tokens=15,  # 含 reasoning token（剧本 usage_metadata 口径）
            cost=0.001,
            latency_ms=88,
        )
    )

    assert row.session_id == session_id
    assert row.run_id == run_id
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (12, 15, 27)
    assert row.status == "success"
    assert row.error_type is None
    assert row.currency == "CNY"
    assert row.created_at is not None and row.created_at.tzinfo is not None


def test_build_model_call_error_row_keeps_error_type() -> None:
    """失败调用也 100% 落行（status=error + error_type），token 口径同上。"""
    row = build_model_call(
        ModelCallEntry(
            session_id=str(uuid4()),
            model="qwen3.7-plus",
            input_tokens=0,
            output_tokens=0,
            status="error",
            error_type="timeout",
        )
    )

    assert (row.status, row.error_type) == ("error", "timeout")


# ---------------------------------------------------------------- 三维聚合（AC-015-2 的纯函数侧）


def _row(
    *,
    session_id: str,
    model: str,
    cost: float,
    created_at: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        model=model,
        cost=cost,
        created_at=created_at,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_aggregate_costs_by_session() -> None:
    rows = [
        _row(
            session_id="s1", model="m1", cost=2.0, created_at=BASE, input_tokens=10, output_tokens=5
        ),
        _row(
            session_id="s1", model="m2", cost=6.0, created_at=BASE, input_tokens=20, output_tokens=0
        ),
        _row(
            session_id="s2", model="m1", cost=1.0, created_at=BASE, input_tokens=0, output_tokens=3
        ),
    ]

    summary = aggregate_costs(rows, "session")

    assert summary.dimension == "session"
    assert len(summary.rows) == 2
    # 按 cost 降序（同额按 group_key 定序）——最贵的会话排最前
    assert [r.group_key for r in summary.rows] == ["s1", "s2"]
    top = summary.rows[0]
    assert (top.model_calls, top.input_tokens, top.output_tokens) == (2, 30, 5)
    assert top.total_tokens == 35
    assert top.cost == pytest.approx(8.0)
    assert top.currency == "CNY"


def test_aggregate_costs_by_date_and_model() -> None:
    day1 = datetime(2026, 10, 17, 8, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 10, 18, 8, 0, 0, tzinfo=UTC)
    rows = [
        _row(
            session_id="s1", model="m1", cost=3.0, created_at=day1, input_tokens=1, output_tokens=1
        ),
        _row(
            session_id="s1", model="m1", cost=2.0, created_at=day2, input_tokens=2, output_tokens=2
        ),
        _row(
            session_id="s2", model="m2", cost=1.0, created_at=day1, input_tokens=4, output_tokens=4
        ),
    ]

    by_date = aggregate_costs(rows, "date")
    assert [(r.group_key, r.model_calls, r.cost) for r in by_date.rows] == [
        ("2026-10-17", 2, 4.0),
        ("2026-10-18", 1, 2.0),
    ]

    by_model = aggregate_costs(rows, "model")
    assert [(r.group_key, r.model_calls, r.total_tokens) for r in by_model.rows] == [
        ("m1", 2, 6),
        ("m2", 1, 8),
    ]
    assert [r.cost for r in by_model.rows] == pytest.approx([5.0, 1.0])


def test_aggregate_costs_empty_rows() -> None:
    summary = aggregate_costs([], "model")

    assert summary.dimension == "model"
    assert summary.rows == []


def test_aggregate_costs_rejects_unknown_dimension() -> None:
    with pytest.raises(ValueError):
        aggregate_costs([], "user")  # 故意给非法维度（聚合函数不校验，契约模型校验拦截）


# ---------------------------------------------------------------- 组装面（与服务层共享的常量）


def test_supported_dimensions_match_contract_enum() -> None:
    """三维口径与契约 CostSummary.dimension 的字面量一致（AC-015-2）。"""
    assert set(cost_service.SUPPORTED_DIMENSIONS) == {"session", "date", "model"}
