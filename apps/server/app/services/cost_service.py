"""成本追踪域服务（A3 §10 任务 8；REQ-015 / AC-015-1~4）。

职责与口径：

- **model_calls 记录**（AC-015-1）：**每次模型调用落一行**（100% 落库，含失败行
  `status=error + error_type`——失败行是审计标记，同源对账基准下残差为 0）。
  **思考 token 计入 output_tokens**（PRD 硬约束 9）——网关的
  `usage_metadata.output_tokens` 已含 reasoning token，本层原样保留、不二次叠加；
  记录粒度 = translator 的逐次调用记账（`ModelCallRecord`）：每收口一条 assistant
  消息落一行，已完成调用不重复计；行集 token 合计与终态 `usage_totals` 同源
  （M4 前置 D5：残差基准 = `translator.usage_totals`，不再取 runtime 单窗口）。
- **三维汇总**（AC-015-2）：`session`（group_key=会话 id）/ `date`（YYYY-MM-DD，UTC）/
  `model`（模型 id）——`model_calls` 行数、token 分项求和、费用求和；行序 = cost 降序、
  同额按 group_key 定序（确定性，测试可复现）。
- **沙箱费用估算**（AC-015-4）：running 段 × `SANDBOX_COST_RATE_PER_HOUR`（1C2G ≈ 0.156
  元/h）+ paused 段 × `SANDBOX_PAUSED_RATE_PER_GIB_HOUR`（元/GiB·h）× 2GiB；进行中的段按
  `now - started_at` 计入（不要求先封口）。金额是**估算值**（契约 `SandboxCostEstimate`
  docstring 口径），落库精度对齐 numeric(12,6)。

分层与协作者：继承 `BaseService`（请求级会话，只 flush 不 commit——事务边界在
`dependencies.get_db`）；模型单价经**注入的注册表**协作者取（运行期不 import `app.agent`，
类型只在 TYPE_CHECKING 下引用，同 session_service 的接缝姿势）；沙箱分段由调用方
（run_service，本车道的管理器持有者）传入——本模块只消费分段快照字段，不 import
`app.sandbox` 的运行期符号（分段类型仅 TYPE_CHECKING 下引用）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import sandbox_settings
from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.exception.session import SessionNotFound
from app.models import ModelCall, SandboxUsage, Session
from app.services.base import BaseService
from lcode_contracts.common import Usage
from lcode_contracts.rest import CostSummary, CostSummaryRow, SandboxCostEstimate, SessionCost

if TYPE_CHECKING:
    from app.agent.models_registry import ModelRegistry
    from app.sandbox.base import SandboxUsageSegment
    from app.sandbox.manager import SandboxUsageRecord

SUPPORTED_DIMENSIONS: tuple[str, ...] = ("session", "date", "model")
"""三维汇总的合法维度（契约 `CostSummary.dimension` 字面量；endpoint 查询参数同源）。"""

DEFAULT_SANDBOX_GIB: float = 2.0
"""P0 沙箱规格 1C2G 的内存侧（GiB）：paused 费率按 元/GiB·h 计价，需乘内存规格。
config 无独立旋钮，P0 固化 1C2G；换规格时改本常量并在 .env.example 注记。"""

MODEL_NOT_PRICED_COST = 0.0
"""未知模型 id 的降级口径：费用按 0 记（token 照常落库）——100% 落库优先于计价精确。"""


# --------------------------------------------------------------------------- 记录面


@dataclass(frozen=True, slots=True)
class ModelCallEntry:
    """一行 model_calls 的入参（一次模型调用一条；收口对账行同用）。"""

    session_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    currency: str = "CNY"
    run_id: str | None = None
    agent_id: str | None = None
    role: str | None = None
    latency_ms: int | None = None
    status: Literal["success", "error"] = "success"
    error_type: str | None = None
    retry_count: int = 0
    user_id: str = "local"
    created_at: datetime | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def build_model_call(entry: ModelCallEntry) -> ModelCall:
    """entry → ORM 行（纯构造，不落库；口径注释见本模块 docstring）。"""
    return ModelCall(
        user_id=entry.user_id,
        session_id=uuid.UUID(entry.session_id),
        run_id=uuid.UUID(entry.run_id) if entry.run_id else None,
        agent_id=entry.agent_id,
        role=entry.role,
        model=entry.model,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        total_tokens=entry.total_tokens,
        latency_ms=entry.latency_ms,
        cost=entry.cost,
        currency=entry.currency,
        status=entry.status,
        error_type=entry.error_type,
        retry_count=entry.retry_count,
        created_at=entry.created_at or datetime.now(UTC),
    )


async def record_model_call_short_tx(
    entry: ModelCallEntry, *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """运行时上下文写一行 model_calls（一次调用一行）：自取短会话立即 commit（A3 §7.3）。

    失败只记日志不抛——AC-015-1 的 100% 落库口径要求留下痕迹，但成本写失败不得拖垮
    run 收口（run 终态事实已在 translator.finish_run 的收口事件里成立）。工厂获取
    同样在 try 内：session factory 构造异常也必须走本函数的日志分支，不得穿到调用方。
    """
    try:
        factory = session_factory or get_session_factory()
        async with factory() as session:
            session.add(build_model_call(entry))
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 计费落库失败不拖垮 run 收口
        BusinessLog(
            message="model_calls 落库失败（run 收口不受影响）",
            action="记录模型成本",
            entity="model_call",
            entity_id=entry.run_id or entry.session_id,
        ).warning(exc)


def model_cost_of(
    *, input_tokens: int, output_tokens: int, input_per_mtok: float, output_per_mtok: float
) -> float:
    """模型调用费用（元）：token/1e6 × 每百万 token 单价，分项计价后求和。"""
    return input_tokens / 1_000_000 * input_per_mtok + output_tokens / 1_000_000 * output_per_mtok


# ------------------------------------------------- 沙箱分段落库（AC-015-4 持久化半边）


def build_sandbox_usage(record: SandboxUsageRecord) -> SandboxUsage:
    """封口段记录 → `sandbox_usage` ORM 行（纯构造，不落库）。

    金额沿用 `estimate_sandbox_cost` 的费率口径（本模块单一费率源：running 段 ×
    SANDBOX_COST_RATE_PER_HOUR；paused 段 × paused 费率 × 1C2G 内存侧），精度对齐
    numeric(12,6)。封口段必有 ended_at，`now` 取其封口时刻不影响结果。
    """
    estimate = estimate_sandbox_cost((record.segment,), now=record.segment.ended_at)
    return SandboxUsage(
        user_id="local",
        session_id=uuid.UUID(record.session_id),
        provider=record.provider,
        sandbox_id=record.sandbox_id,
        phase=record.segment.phase,
        started_at=record.segment.started_at,
        ended_at=record.segment.ended_at,
        duration_seconds=record.segment.duration_seconds,
        estimated_cost=estimate.accrued,
        currency=record.segment.currency,
    )


async def record_sandbox_usage_short_tx(
    record: SandboxUsageRecord, *, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """运行时上下文写一行 sandbox_usage（封口段）：自取短会话立即 commit（A3 §7.3）。

    失败只记日志不抛——分段落库是审计/跨重启口径的辅助事实（事件流与查询面估算的
    数据源是 manager 内存账本），写失败不得拖垮沙箱相位机或 run 链路。
    """
    factory = session_factory or get_session_factory()
    try:
        async with factory() as session:
            session.add(build_sandbox_usage(record))
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 分段落库失败不拖垮生命周期
        BusinessLog(
            message="sandbox_usage 分段落库失败（生命周期不受影响）",
            action="记录沙箱分段",
            entity="sandbox_usage",
            entity_id=record.sandbox_id,
        ).warning(exc)


# --------------------------------------------------------------------------- 沙箱估算（AC-015-4）


def estimate_sandbox_cost(
    segments: Sequence[SandboxUsageSegment],
    *,
    open_segment: SandboxUsageSegment | None = None,
    now: datetime | None = None,
    rate_per_hour: float | None = None,
    paused_rate_per_gib_hour: float | None = None,
) -> SandboxCostEstimate:
    """分段（closed + 进行中）→ 沙箱费用估算（纯函数；金额 = Σ 时长 × 费率）。

    - running 段：`SANDBOX_COST_RATE_PER_HOUR`（元/小时，1C2G 口径）；
    - paused 段：`SANDBOX_PAUSED_RATE_PER_GIB_HOUR` × `DEFAULT_SANDBOX_GIB`（保留保留费）；
    - 进行中（`ended_at is None`）的段按 `now - started_at` 计入——查询面不需要先封口；
    - `rate_per_hour` 如实回传运行单价（契约 SandboxCostEstimate.rate_per_hour 语义）。
    """
    running_rate = sandbox_settings.cost_rate_per_hour if rate_per_hour is None else rate_per_hour
    paused_rate = (
        sandbox_settings.paused_rate_per_gib_hour
        if paused_rate_per_gib_hour is None
        else paused_rate_per_gib_hour
    )
    current = now or datetime.now(UTC)

    accrued = 0.0
    for segment in (*segments, *((open_segment,) if open_segment is not None else ())):
        if segment.ended_at is not None:
            seconds = segment.duration_seconds
            if seconds is None:  # 封口段缺时长（异常数据）：按起止差兜底
                seconds = max(int((segment.ended_at - segment.started_at).total_seconds()), 0)
        else:
            seconds = max(int((current - segment.started_at).total_seconds()), 0)
        if segment.phase == "running":
            accrued += seconds / 3600 * running_rate
        else:  # paused
            accrued += seconds / 3600 * paused_rate * DEFAULT_SANDBOX_GIB

    return SandboxCostEstimate(
        rate_per_hour=running_rate,
        accrued=round(accrued, 6),  # numeric(12,6) 的落库精度口径
        currency="CNY",
    )


# --------------------------------------------------------------------------- 三维聚合（AC-015-2）


class _AggregateRow(Protocol):
    """聚合输入的最小字段面（ModelCall ORM 行天然满足；测试可用替身）。"""

    session_id: Any
    model: str
    input_tokens: int
    output_tokens: int
    cost: Any
    created_at: datetime


def _group_key(row: _AggregateRow, dimension: str) -> str:
    if dimension == "session":
        return str(row.session_id)
    if dimension == "date":
        return row.created_at.astimezone(UTC).strftime("%Y-%m-%d")
    return row.model


def aggregate_costs(rows: Sequence[_AggregateRow], dimension: str) -> CostSummary:
    """行集合 → 三维 `CostSummary`（纯函数；SQL 侧行集由 CostService.summary 提供）。

    行序 = cost 降序、同额按 group_key 升序（确定性）；`model_calls` 计**行数**
    （100% 落库口径，含失败行——失败行是同源基准下的 0 残差审计标记）。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _group_key(row, dimension)
        bucket = buckets.setdefault(
            key,
            {"count": 0, "input": 0, "output": 0, "cost": 0.0},
        )
        bucket["count"] += 1
        bucket["input"] += int(row.input_tokens)
        bucket["output"] += int(row.output_tokens)
        bucket["cost"] += float(row.cost)

    ordered = sorted(buckets.items(), key=lambda item: (-item[1]["cost"], item[0]))
    return CostSummary(
        dimension=dimension,  # type: ignore[arg-type]  # 调用方传契约三字面量之一
        rows=[
            CostSummaryRow(
                group_key=key,
                model_calls=bucket["count"],
                input_tokens=bucket["input"],
                output_tokens=bucket["output"],
                total_tokens=bucket["input"] + bucket["output"],
                cost=round(bucket["cost"], 6),
                currency="CNY",
            )
            for key, bucket in ordered
        ],
    )


# --------------------------------------------------------------------------- 服务


class CostService(BaseService):
    """成本域业务逻辑：请求侧读（汇总/会话成本）与请求侧补记（flush，事务在 get_db）。"""

    def __init__(self, db: AsyncSession, registry: ModelRegistry | None = None) -> None:
        super().__init__(db)
        self._registry = registry

    # ---------------------------------------------------------------- 记录

    async def record_model_call(self, entry: ModelCallEntry) -> None:
        """请求上下文补记一行 model_calls（flush；提交交由 get_db 统一处理）。"""
        self.db.add(build_model_call(entry))
        await self.db.flush()

    def cost_for(self, model_id: str, usage: Usage) -> float:
        """按注册表单价估算费用；未知模型降级为 0（token 照常计入，NFR-004 不泄漏 id 细节）。"""
        if self._registry is None:
            return MODEL_NOT_PRICED_COST
        try:
            pricing = self._registry.pricing(model_id)
        except Exception as exc:  # noqa: BLE001 —— ModelNotFound：降级不中断 run 收口
            BusinessLog(
                message="模型单价缺失，费用按 0 记",
                action="估算模型成本",
                entity="model",
                entity_id=model_id,
            ).warning(exc)
            return MODEL_NOT_PRICED_COST
        return model_cost_of(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_per_mtok=pricing.input_per_mtok,
            output_per_mtok=pricing.output_per_mtok,
        )

    # ---------------------------------------------------------------- 查询

    async def summary(self, dimension: str) -> CostSummary:
        """三维汇总（AC-015-2）：维度合法性与聚合口径见 `aggregate_costs`。"""
        if dimension not in SUPPORTED_DIMENSIONS:
            from app.exception.common import InvalidArgument

            raise InvalidArgument(
                f"汇总维度非法：{dimension}", detail=f"支持 {list(SUPPORTED_DIMENSIONS)}"
            )
        rows = list(await self.db.scalars(self._model_call_query()))
        return aggregate_costs(rows, dimension)

    async def session_cost(self, session_id: str, *, sandbox: SandboxCostEstimate) -> SessionCost:
        """会话成本（AC-015-3）：模型段（token + 费用）+ 调用方给的沙箱段估算。

        沙箱分段由 run_service（管理器持有者）估算后传入——本方法只负责模型段与会话
        存在性校验，避免本模块耦合沙箱管理器。
        """
        row = await self._require_session(session_id)
        calls = list(await self.db.scalars(self._model_call_query(row.id)))
        usage = Usage(
            input_tokens=sum(int(call.input_tokens) for call in calls),
            output_tokens=sum(int(call.output_tokens) for call in calls),
        )
        model_cost = sum(float(call.cost) for call in calls)
        return SessionCost(model=usage, model_cost=round(model_cost, 6), sandbox=sandbox)

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _model_call_query(session_id: uuid.UUID | None = None) -> Select[tuple[ModelCall]]:
        statement = select(ModelCall)
        if session_id is not None:
            statement = statement.where(ModelCall.session_id == session_id)
        return statement

    async def _require_session(self, session_id: str) -> Session:
        """会话存在性校验（含非法 UUID 同口径）——与 session_service 同一错误码约定。"""
        try:
            key = uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFound(
                f"会话不存在：{session_id}", detail="路径参数不是合法 UUID", original_exception=exc
            ) from exc
        row = await self.db.scalar(select(Session).where(Session.id == key))
        if row is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        return row


__all__ = [
    "DEFAULT_SANDBOX_GIB",
    "MODEL_NOT_PRICED_COST",
    "SUPPORTED_DIMENSIONS",
    "CostService",
    "ModelCallEntry",
    "aggregate_costs",
    "build_model_call",
    "build_sandbox_usage",
    "estimate_sandbox_cost",
    "model_cost_of",
    "record_model_call_short_tx",
    "record_sandbox_usage_short_tx",
]
