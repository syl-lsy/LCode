"""会话域服务（A3 §7/§8；M2 任务 7 会话车道）。

职责（REQ-004 / REQ-014）：

- **创建**：标题/模型/档位/审批模式落库；模型经注册表校验（未知模型 → `ModelNotFound`）；
  `sandbox_provider` 固化**当前配置值**（AC-002-6：改全局配置只影响新会话）；
- **列表**：按最近活跃倒序 + 标题子串搜索（AC-004-3），`total` 为过滤后总数；
- **详情**：投影契约 `SessionDetail`；`run_state` 由 runs 表推导
  （idle / running / waiting_approval，AC-006-5 刷新恢复的关键状态）；
- **PATCH**：模型/档位热切换写回会话行（AC-014-2：run 启动时重读会话行 →「下一条消息
  起生效」天然成立）；切到不支持 reasoning 的模型且未显式给档位时档位回落 `"off"`
  （AC-014-3）。

档位裁定规则（本层唯一实现处，create 与 patch 共用）：

- **显式给了档位** → 必须落在该模型声明支持集 `reasoning_efforts` 内，否则
  `InvalidArgument`（契约 create/patch 的 errors 均含 INVALID_ARGUMENT）；模型
  `supports_reasoning=False` 时显式档位一律拒绝（前端置灰的等价后端行为）；
- **未显式给** → 按候选序取第一个被该模型支持的档位（create：角色级 `roles.main` 默认；
  patch：现有档位 → 角色级默认）；模型完全不支持 reasoning 时恒 `"off"`。

分层约定（基座铁律 10）：**DTO 进 DTO 出**（wire DTO 直接复用契约类型，见 `app.schemas`）、
**只 flush 不 commit**（事务边界统一在 `dependencies.get_db`）；ORM 行不逃出本层
（一律经 `_to_detail` 投影）。查询全部走 SQLAlchemy 表达式（条件即绑定参数，标题搜索用
`icontains(autoescape=True)`——转义与参数绑定都由 SQLAlchemy 完成，不手工拼 LIKE 模式）。
模型注册表以协作者注入（api 经 `dependencies.ModelRegistryDep` 传入）；**运行期不 import
`app.agent`**（A3 §1.2 的单向依赖），注册表类型只在 `TYPE_CHECKING` 下引用。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import sandbox_settings
from app.core.db import get_session_factory
from app.core.logger import BusinessLog, service_logger
from app.exception.common import InvalidArgument
from app.exception.session import SessionNotFound
from app.models import Run, Session
from app.schemas import SessionCreate, SessionDetail, SessionPage, SessionPatch
from app.services.base import BaseService

if TYPE_CHECKING:
    from app.agent.models_registry import ModelRegistry, ModelSpec

ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "waiting_approval")
"""活动 run 状态（契约 runs.status 5 值里的两个非终态）：其余三值不参与 run_state 推导。"""

MAIN_ROLE = "main"
"""会话级默认档位的取值角色：主对话（A3 §3.1 优先级链的「角色级」兜底位）。"""

SUPPORTED_SANDBOX_PROVIDERS: tuple[str, ...] = ("bailian", "opensandbox")
"""契约 `rest.SandboxProvider` 值域（与 sessions.sandbox_provider 的 CHECK 约束同源）。"""


class SessionService(BaseService):
    """会话域业务逻辑：构造注入请求级会话 + 模型注册表（不自己建会话/引擎）。"""

    def __init__(self, db: AsyncSession, registry: ModelRegistry) -> None:
        super().__init__(db)
        self._registry = registry

    # ---------------------------------------------------------------- 对外方法

    @service_logger(
        "创建会话",
        entity="session",
        id_extractor=lambda args, kwargs, result: getattr(result, "id", ""),
    )
    async def create_session(self, payload: SessionCreate) -> SessionDetail:
        """创建会话：模型/档位校验后落库一行（`sandbox_provider` 取创建期配置快照）。"""
        spec = self._registry.spec(payload.model or self._registry.default_model)
        effort = (
            self._explicit_effort(spec, payload.reasoning_effort)
            if payload.reasoning_effort is not None
            else self._default_effort(spec, self._role_effort())
        )

        row = Session(
            model=spec.id,
            reasoning_effort=effort,
            sandbox_provider=self._sandbox_provider(),
        )
        if payload.title is not None:
            row.title = payload.title
        if payload.approval_mode is not None:
            row.approval_mode = payload.approval_mode
        # title/approval_mode 未给时不写列：让 DB 服务端默认（'新会话' / 'manual'）单点生效

        self.db.add(row)
        await self.db.flush()  # 拿自增 id + 触发 CHECK/NOT NULL 校验；提交交由 get_db
        return self._to_detail(row, run_state="idle")

    async def list_sessions(self, *, title: str | None = None) -> SessionPage:
        """会话列表：最近活跃倒序（`last_active_at` → `created_at` → `id` 定序）+ 标题子串搜索。

        `total` 是**过滤后**总数（契约 SessionPage.total 语义），不是全表行数。
        搜索条件经 `icontains(autoescape=True)`：`%`/`_` 按字面处理，条件即绑定参数。
        """
        conditions = [Session.title.icontains(title, autoescape=True)] if title else []
        total = await self.db.scalar(select(func.count()).select_from(Session).where(*conditions))
        statement = self._ordered().where(*conditions)
        rows = list(await self.db.scalars(statement))

        states = await self._run_states([row.id for row in rows])
        return SessionPage(
            items=[self._to_detail(row, states.get(row.id, "idle")) for row in rows],
            total=total or 0,
        )

    async def get_session(self, session_id: str) -> SessionDetail:
        """会话详情（含 run_state）；不存在 → `SessionNotFound`。"""
        row = await self._require_row(session_id)
        states = await self._run_states([row.id])
        return self._to_detail(row, states.get(row.id, "idle"))

    @service_logger(
        "更新会话",
        entity="session",
        id_extractor=lambda args, kwargs, result: getattr(result, "id", ""),
    )
    async def patch_session(self, session_id: str, payload: SessionPatch) -> SessionDetail:
        """更新会话（模型/档位热切换、标题、审批模式）；只动请求里出现的字段。

        档位（`reasoning_effort`）不与模型同给时按**当前模型**裁定；与模型同给时按**新模型**
        裁定。`None` 值视为「本次不改该字段」（会话行各列 NOT NULL，没有「清空」语义）。
        """
        row = await self._require_row(session_id)

        if payload.title is not None:
            row.title = payload.title
        if payload.approval_mode is not None:
            row.approval_mode = payload.approval_mode

        if payload.model is not None:
            spec = self._registry.spec(payload.model)
            row.model = spec.id
            if payload.reasoning_effort is not None:
                row.reasoning_effort = self._explicit_effort(spec, payload.reasoning_effort)
            else:
                # 切模型不带档位：现档位仍被新模型支持就保留，否则回落角色级默认 → "off"
                row.reasoning_effort = self._default_effort(
                    spec, row.reasoning_effort, self._role_effort()
                )
        elif payload.reasoning_effort is not None:
            row.reasoning_effort = self._explicit_effort(
                self._registry.spec(row.model), payload.reasoning_effort
            )

        # updated_at 无 onupdate：PATCH 显式推进（时间取 Python 侧，测试可断言单调）
        row.updated_at = datetime.now(UTC)
        await self.db.flush()

        states = await self._run_states([row.id])
        return self._to_detail(row, states.get(row.id, "idle"))

    # ---------------------------------------------------------------- 内部：档位裁定

    @staticmethod
    def _explicit_effort(spec: ModelSpec, effort: str) -> str:
        """显式档位校验：模型不支持 reasoning，或档位不在其支持集内 → InvalidArgument。"""
        if not spec.supports_reasoning:
            raise InvalidArgument(
                f"模型 {spec.id} 不支持 reasoning 档位",
                detail="supports_reasoning=false 的模型不接受显式档位（AC-014-3）",
            )
        if effort not in spec.reasoning_efforts:
            raise InvalidArgument(
                f"档位 {effort} 不被模型 {spec.id} 支持",
                detail=f"{spec.id} 的支持档位：{list(spec.reasoning_efforts)}",
            )
        return effort

    @staticmethod
    def _default_effort(spec: ModelSpec, *candidates: str | None) -> str:
        """未显式给档位时的裁定：候选序里第一个被模型支持的档位；不支持 reasoning 恒 "off"。"""
        if not spec.supports_reasoning:
            return "off"
        for candidate in candidates:
            if candidate is not None and candidate in spec.reasoning_efforts:
                return candidate
        return "off"

    def _role_effort(self) -> str | None:
        """角色级默认档位（`roles.main`）：会话未显式给档位时的兜底候选。"""
        return self._registry.resolve(MAIN_ROLE).effort

    @staticmethod
    def _sandbox_provider() -> str:
        """创建期固化当前 `SANDBOX_PROVIDER`（AC-002-6）；配置值非法 → InvalidArgument。

        值域在 DB 侧还有 CHECK 兜底；这里前置校验是为了给出「配置写错了」的可读错误，
        而不是把 IntegrityError 冒泡成 INTERNAL。
        """
        provider = sandbox_settings.provider
        if provider not in SUPPORTED_SANDBOX_PROVIDERS:
            raise InvalidArgument(
                f"沙箱 provider 配置非法：{provider}",
                detail=f"SANDBOX_PROVIDER 只支持 {list(SUPPORTED_SANDBOX_PROVIDERS)}",
            )
        return provider

    # ---------------------------------------------------------------- 内部：查询与投影

    async def _require_row(self, session_id: str) -> Session:
        """按 wire id 取会话行；id 非法或不存在同口径 → `SessionNotFound`。

        契约上会话族端点只声明 SESSION_NOT_FOUND（无 INVALID_ARGUMENT），故 id 解析失败
        也走同一码——客户端视角都是「这个会话不存在」。
        """
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

    async def _run_states(self, session_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """批量取每个会话的**最新活动 run** 状态；无活动 run 的会话不出现在结果里。

        一次查询覆盖整页（列表不逐行查库）；按 `started_at` 倒序首见即最新（P0 单会话至多
        一个活动 run，`setdefault` 兜住异常数据）。
        """
        if not session_ids:
            return {}
        rows = await self.db.scalars(
            select(Run)
            .where(Run.session_id.in_(session_ids), Run.status.in_(ACTIVE_RUN_STATUSES))
            .order_by(Run.started_at.desc(), Run.created_at.desc())
        )
        states: dict[uuid.UUID, str] = {}
        for run in rows:
            states.setdefault(run.session_id, run.status)
        return states

    @staticmethod
    def _ordered() -> Select[tuple[Session]]:
        """列表排序：最近活跃优先；同活跃时间的按创建时间、id 定序（分页/断言可复现）。"""
        return select(Session).order_by(
            Session.last_active_at.desc(), Session.created_at.desc(), Session.id.desc()
        )

    @staticmethod
    def _to_detail(row: Session, run_state: str) -> SessionDetail:
        """ORM 行 → 契约 `SessionDetail`（就地转换，含契约值域校验；ORM 不逃出 service 层）。"""
        return SessionDetail.model_validate(
            {
                "id": str(row.id),
                "title": row.title,
                "status": row.status,
                "model": row.model,
                "reasoning_effort": row.reasoning_effort,
                "approval_mode": row.approval_mode,
                "sandbox_provider": row.sandbox_provider,
                "run_state": run_state,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "last_active_at": row.last_active_at,
            }
        )


async def update_session_sandbox_id_short_tx(
    session_id: str,
    sandbox_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """运行时上下文写 `sessions.sandbox_id`（沙箱相位 running/resumed/recreated 时；S3）。

    短事务模式（同 `cost_service.record_sandbox_usage_short_tx`）：自取短会话立即
    commit。它是「进程重启后按持久 id adopt 重连」的辅助事实源（权威事实在 manager
    内存账本与事件流），**写失败只记日志不抛**——不得拖垮相位机或 run 链路；会话行
    已不存在（并发删除）按 no-op 处理。
    """
    factory = session_factory or get_session_factory()
    try:
        async with factory() as session:
            try:
                key = uuid.UUID(session_id)
            except ValueError:
                return  # 非法 id 不落库也不报错（调用面传来的恒是 wire id）
            row = await session.get(Session, key)
            if row is None:
                return
            row.sandbox_id = sandbox_id
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 持久 id 落库失败不拖垮生命周期
        BusinessLog(
            message=(
                "sessions.sandbox_id 落库失败（重启后首工具调用的懒 adopt 可能失效："
                "持久 id 未更新，届时将降级 create 重建）"
            ),
            action="记录沙箱持久id",
            entity="session",
            entity_id=session_id,
        ).warning(exc)
