"""unit 层：run 收口 → model_calls 逐次落库（AC-015-1；零库、零网络）。

本文件只测 `RunService._finalize` 的记账编排：假 sink 提供 translator 记账快照，
monkeypatch 掉短事务落行、run 行收口与既往段落用量查询，断言**收集到的
ModelCallEntry** 与终态 usage_totals。库侧行为（行落库/三维汇总）在
integration/test_rest_api.py 覆盖。

锁住的不变量：
- 一次模型调用一行（translator 记录逐条映射）；
- role 映射：main / general-purpose → main，explorer/coder/reviewer 同名，未知与 None
  → main（role 永不为 NULL）；
- **对账基准与逐次记账同源**：残差 = translator.usage_totals − 行集合计（不再是
  `outcome.usage`——runtime 单窗口在并行子智能体交错流式时会失真，M4 前置 D5）；
- error 路径必定追加恰好一条 error 残差行（本段残差恒为 0：行集已同源），全部行
  token 之和 == translator.usage_totals；`outcome.usage` 只作运行诊断；
- 正常路径（行集合计 == translator 累计）不产生多余对账行；
- 跨段终态 usage_totals = 既往段落合计 + 本段 translator 累计；
- 注册表解析失败回退 launch.model 并留 warning，不丢行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from loguru import logger

from app.agent.models_registry import ModelRegistry
from app.agent.runtime import RunOutcome
from app.events.translator import ModelCallRecord
from app.services import run_service as run_service_module
from app.services.cost_service import ModelCallEntry
from app.services.message_service import RunLaunch
from app.services.run_service import RunService
from lcode_contracts.common import Usage
from lcode_contracts.errors import RunErrorObj

SESSION_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def _launch(model: str = "qwen3.7-plus", effort: str = "high") -> RunLaunch:
    return RunLaunch(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        content="你好",
        model=model,
        reasoning_effort=effort,
        provider="bailian",
    )


def _record(
    *,
    message_id: str = "m1",
    started_at: datetime = BASE,
    duration_ms: int = 250,
    input_tokens: int = 10,
    output_tokens: int = 5,
    agent_id: str | None = None,
    role: str | None = "main",
) -> ModelCallRecord:
    return ModelCallRecord(
        message_id=message_id,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=duration_ms),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        agent_id=agent_id,
        role=role,
    )


@dataclass(frozen=True)
class _FakeTranslator:
    """translator 的用量投影：`usage_totals` 与逐次记账快照同源（Σ records）。"""

    records: tuple[ModelCallRecord, ...]

    @property
    def usage_totals(self) -> Usage:
        return Usage(
            input_tokens=sum(record.input_tokens for record in self.records),
            output_tokens=sum(record.output_tokens for record in self.records),
        )


@dataclass
class _FakeSink:
    """假 sink：`finish` 记序并留存 usage_totals，`model_calls`/`translator` 直出记账快照。

    `finish` 的 `usage_offset` 语义与生产侧一致：既往段落合计 + **收口后**的本段
    translator 累计（unit 层没有在飞消息，故本段累计即 records 合计）。
    """

    records: tuple[ModelCallRecord, ...]
    order: list[str]
    model_calls_recorded: bool = False
    usage_totals: Usage | None = None

    async def finish(
        self, phase: str, *, error: Any = None, usage_offset: Usage | None = None
    ) -> None:
        self.order.append("finish")
        if phase != "finished":
            self.usage_totals = None
            return
        segment = self.translator.usage_totals
        offset = usage_offset if usage_offset is not None else Usage()
        self.usage_totals = Usage(
            input_tokens=segment.input_tokens + offset.input_tokens,
            output_tokens=segment.output_tokens + offset.output_tokens,
        )

    @property
    def translator(self) -> _FakeTranslator:
        return _FakeTranslator(self.records)

    @property
    def model_calls(self) -> tuple[ModelCallRecord, ...]:
        return self.records

    def mark_model_calls_recorded(self) -> None:
        self.model_calls_recorded = True


class _BoomRegistry:
    """解析必失败的注册表替身（验证回退 launch.model 且不丢行）。"""

    def resolve(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("registry exploded")


def _service(registry: Any | None) -> RunService:
    # _finalize 不触碰 self.db（行/事件各有短事务出口）；零库构造是刻意的
    return RunService(
        cast(Any, None),
        pipeline=cast(Any, None),
        manager=cast(Any, None),
        model_registry=registry,
    )


def _collect(
    monkeypatch: Any, order: list[str], *, prior: Usage | None = None
) -> list[ModelCallEntry]:
    """收集本段落行；`prior` 注入「既往段落 token 合计」（缺省 0）。

    `_prior_model_usage` 必须打桩：真实现会经 `get_session_factory()` 连库，而 unit
    层要求零网络。放行它会把这台单测第一条 asyncpg 连接留在**本测事件循环**的连接池里，
    后续 integration 复用进程级 engine 单例时跨 loop 取到旧连接（Future attached to a
    different loop）。跨段合计语义用注入值验证，不靠真实查询。
    """
    entries: list[ModelCallEntry] = []
    prior_usage = prior if prior is not None else Usage()

    async def _record_entry(entry: ModelCallEntry, **_kwargs: Any) -> None:
        entries.append(entry)
        order.append("row")

    async def _close_run_row(_self: Any, _run_id: str, _outcome: RunOutcome) -> None:
        order.append("run_row")

    async def _prior(_self: Any, _run_id: str) -> Usage:
        return prior_usage

    monkeypatch.setattr(run_service_module, "record_model_call_short_tx", _record_entry)
    monkeypatch.setattr(RunService, "_close_run_row", _close_run_row)
    monkeypatch.setattr(RunService, "_prior_model_usage", _prior)
    return entries


async def test_finalize_records_one_row_per_model_call(monkeypatch: Any) -> None:
    """两条 translator 记录 → 两行；字段映射（token/agent/role/model/延迟/created_at）。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    second_started = BASE + timedelta(seconds=1)
    sink = _FakeSink(
        records=(
            # 主智能体记录可能带内层 deepagents 的 execute 命名空间路径 → 落库归一为 None
            _record(message_id="m1", input_tokens=10, output_tokens=5, agent_id="execute:abc"),
            _record(
                message_id="m2",
                started_at=second_started,
                duration_ms=400,
                input_tokens=20,
                output_tokens=7,
                agent_id="worker:def",
                role="explorer",
            ),
        ),
        order=order,
    )

    outcome = RunOutcome(
        status="finished",
        usage=Usage(input_tokens=30, output_tokens=12),
        error=None,
        duration_ms=900,
    )
    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert len(entries) == 2
    first, second = entries
    assert (first.input_tokens, first.output_tokens) == (10, 5)
    assert (first.role, first.agent_id, first.status) == ("main", None, "success")
    assert first.model == "qwen3.7-plus"
    assert first.latency_ms == 250
    assert first.created_at == BASE
    assert (second.role, second.agent_id) == ("explorer", "worker:def")
    assert second.latency_ms == 400
    assert second.created_at == second_started
    # 顺序：收口事件 → 逐条成本行 → run 行终态（事件可回放优先）
    assert order == ["finish", "row", "row", "run_row"]


async def test_finalize_maps_general_purpose_to_main(monkeypatch: Any) -> None:
    """general-purpose 子智能体在成本行归主角色（role 永不为 NULL）。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(records=(_record(role="general-purpose", agent_id="worker:gp"),), order=order)
    outcome = RunOutcome(
        status="finished", usage=Usage(input_tokens=10, output_tokens=5), error=None, duration_ms=1
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert len(entries) == 1
    assert entries[0].role == "main"
    assert entries[0].agent_id == "worker:gp"  # 子智能体身份保留（role 映射不改归属）


async def test_finalize_maps_unknown_and_none_role_to_main(monkeypatch: Any) -> None:
    """未知角色与 None 都回落 main（DB role 非空口径）。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(
        records=(_record(message_id="m1", role="mystery"), _record(message_id="m2", role=None)),
        order=order,
    )
    outcome = RunOutcome(
        status="finished", usage=Usage(input_tokens=20, output_tokens=10), error=None, duration_ms=1
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert [entry.role for entry in entries] == ["main", "main"]


async def test_finalize_error_path_adds_single_residual_error_row(monkeypatch: Any) -> None:
    """error：已完成调用不重复计，恒补一条 error 行（同源基准下残差为 0，不再凭 outcome 造数）。

    M4 前置 D5：对账基准切到 `translator.usage_totals` 后，已完成调用已覆盖本段全部
    用量 → 残差行 token = 0；该行的语义是「本 run 以失败收场」的审计标记（status/
    error_type/latency），不承担补 token 的职责。`outcome.usage`（runtime 单窗口）
    只作运行诊断，留 40/20 也不得改写行集。
    """
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(
        records=(
            _record(message_id="m1", input_tokens=10, output_tokens=5),
            _record(
                message_id="m2",
                input_tokens=20,
                output_tokens=7,
                agent_id="worker:def",
                role="coder",
            ),
        ),
        order=order,
    )
    outcome = RunOutcome(
        status="error",
        usage=Usage(input_tokens=40, output_tokens=20),
        error=RunErrorObj(code="model_error", message="模型网关调用失败", retryable=True),
        duration_ms=500,
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert len(entries) == 3
    error_rows = [entry for entry in entries if entry.status == "error"]
    assert len(error_rows) == 1
    residual = error_rows[0]
    assert (residual.input_tokens, residual.output_tokens) == (0, 0)  # 同源基准：残差恒 0
    assert residual.error_type == "model_error"
    assert residual.latency_ms == 500
    assert residual.role == "main" and residual.agent_id is None
    # 守恒：行集合计 == translator 累计（30/12），不受 outcome.usage（40/20）影响
    assert sum(entry.input_tokens for entry in entries) == 30
    assert sum(entry.output_tokens for entry in entries) == 12


async def test_finalize_normal_path_adds_no_reconciliation_row(monkeypatch: Any) -> None:
    """正常路径：行集合计 == translator 累计 → 不产生多余对账行。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(
        records=(
            _record(message_id="m1", input_tokens=10, output_tokens=5),
            _record(message_id="m2", input_tokens=20, output_tokens=7, role="reviewer"),
        ),
        order=order,
    )
    outcome = RunOutcome(
        status="finished",
        usage=Usage(input_tokens=30, output_tokens=12),
        error=None,
        duration_ms=900,
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert len(entries) == 2
    assert all(entry.status == "success" for entry in entries)
    # 终态事件 usage_totals = prior（缺省 0）+ 本段 translator 累计
    assert sink.usage_totals == Usage(input_tokens=30, output_tokens=12)


async def test_finalize_ignores_diagnostic_outcome_usage_for_cost_rows(monkeypatch: Any) -> None:
    """`outcome.usage` 只作运行诊断：与行集不同源时，行集/终态事件都不得被它改写。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(
        records=(_record(message_id="m1", input_tokens=10, output_tokens=5),),
        order=order,
    )
    outcome = RunOutcome(
        status="finished",
        usage=Usage(input_tokens=99, output_tokens=88),  # 单窗口失真（M4 fan_out 场景）
        error=None,
        duration_ms=1,
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    assert [(entry.input_tokens, entry.output_tokens) for entry in entries] == [(10, 5)]
    assert sink.usage_totals == Usage(input_tokens=10, output_tokens=5)


async def test_finalize_usage_totals_adds_prior_segments(monkeypatch: Any) -> None:
    """跨段合计：终态 usage_totals = 既往段落合计 + 本段 translator 累计（resume 前不少计）。

    D4 单元级锁定：prior=(100, 50) + 本段 (10, 5) → sink.finish 收到 (110, 55)。
    本段用量由 translator 在补收在飞消息后自行计入（sink 以 `usage_offset` 提交基数）。
    """
    order: list[str] = []
    entries = _collect(monkeypatch, order, prior=Usage(input_tokens=100, output_tokens=50))
    sink = _FakeSink(records=(_record(input_tokens=10, output_tokens=5),), order=order)
    outcome = RunOutcome(
        status="finished",
        usage=Usage(input_tokens=10, output_tokens=5),
        error=None,
        duration_ms=1,
    )

    await _service(ModelRegistry.load())._finalize(_launch(), outcome, cast(Any, sink))

    # prior 只进终态事件：本段逐次记账行不受既往段落影响（否则会重复计 token）
    assert [(entry.input_tokens, entry.output_tokens) for entry in entries] == [(10, 5)]
    assert sink.usage_totals == Usage(input_tokens=110, output_tokens=55)


async def test_finalize_registry_failure_falls_back_to_launch_model(monkeypatch: Any) -> None:
    """注册表解析失败：回退 launch.model、留 WARNING，且不丢行。"""
    order: list[str] = []
    entries = _collect(monkeypatch, order)
    sink = _FakeSink(records=(_record(),), order=order)
    outcome = RunOutcome(
        status="finished", usage=Usage(input_tokens=10, output_tokens=5), error=None, duration_ms=1
    )

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        await _service(_BoomRegistry())._finalize(_launch(), outcome, cast(Any, sink))
    finally:
        logger.remove(sink_id)

    assert len(entries) == 1
    assert entries[0].model == "qwen3.7-plus"  # launch.model 回退
    warnings = [
        record
        for record in captured
        if record["level"].name == "WARNING" and record["extra"].get("entity") == "model"
    ]
    assert warnings, "注册表解析失败必须留 WARNING 日志"


async def test_prior_model_usage_falls_back_to_zero_when_query_fails(
    monkeypatch: Any,
) -> None:
    """既往用量查询失败 → 返回 0/0 且不抛出（fail-safe；零真库）。

    `_prior_model_usage` 是跨段 `usage_totals` 的「既往」部分；统计查询失败绝不能让
    终态事件丢失，因此按 0 记并留 WARNING。
    """

    def _boom() -> Any:
        raise RuntimeError("session factory exploded")

    monkeypatch.setattr(run_service_module, "get_session_factory", _boom)
    service = _service(None)

    captured: list[Any] = []
    sink_id = logger.add(
        lambda message: captured.append(message.record), format="{message}", level=0
    )
    try:
        usage = await service._prior_model_usage(RUN_ID)
    finally:
        logger.remove(sink_id)

    assert usage == Usage()
    warnings = [
        record
        for record in captured
        if record["level"].name == "WARNING" and record["extra"].get("entity") == "run"
    ]
    assert warnings, "查询失败回退必须留 WARNING 日志"
