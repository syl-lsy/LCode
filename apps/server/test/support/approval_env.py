"""审批流 integration 共用装置（真 PG / 真事件管线；假模型 + FakeSandbox + InMemorySaver）。

两层审批用例（`test_approval_flow.py` / `test_subagent_approval.py`）共用本模块：

- `approval_env`：运行时三依赖的替身（沙箱管理器 / checkpointer / 模型工厂），事件管线
  走真件（真 DB 短事务 + 真 seq 分配 + 真广播）——审批事件「先落库后广播」是本图层要验
  的真实语义；
- 轮询/查询助手：`wait_run_state`（走 REST 的 run_state 投影，不碰内部实现）、
  `latest_run` / `approvals_of` / `events_of`（走测试会话读已提交事实）。

库依赖：requires_db（integration conftest 重建 DB_NAME 指向的测试库）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_event_pipeline
from app.models import Approval, ModelCall
from app.models import Event as EventRow
from app.models import Message as MessageRow
from app.models import Run as RunRow
from app.sandbox.manager import SandboxManager
from lcode_contracts.errors import SUCCESS_CODE
from lcode_contracts.rest import SessionDetail
from test.support.fake_llm import ScriptedChatModel
from test.support.fake_sandbox import FakeSandbox

SESSIONS = "/api/v1/sessions"
APPROVALS = "/api/v1/approvals"


class ApprovalEnv:
    """运行时替身集合（与 test_rest_api 的 _AgentEnv 同口径，模型工厂支持按角色分派）。"""

    def __init__(self, tmp_path) -> None:
        self.sandboxes: dict[str, FakeSandbox] = {}

        def _factory(provider: str, session_id: str) -> FakeSandbox:  # noqa: ARG001
            sandbox = FakeSandbox(root=tmp_path / session_id, session_id=session_id)
            self.sandboxes[session_id] = sandbox
            return sandbox

        self.manager = SandboxManager(factory=_factory)
        self.pipeline = get_event_pipeline()
        self._default: ScriptedChatModel | None = None
        self._role_models: dict[str, ScriptedChatModel] = {}

    def set_model(self, model: ScriptedChatModel) -> None:
        """所有角色共用同一剧本模型（主智能体单线场景）。"""
        self._default = model
        self._role_models = {}

    def set_role_models(self, models: Mapping[str, ScriptedChatModel]) -> None:
        """按角色分派模型（子智能体场景：main 与 coder 各一份剧本）。"""
        self._role_models = dict(models)

    def model_factory(self, role: str) -> ScriptedChatModel:
        """按角色取模型；未登记的角色回落 main（装配期会为四个角色各取一次）。"""
        model = self._role_models.get(role) or self._role_models.get("main") or self._default
        if model is None:
            raise AssertionError(f"测试未设置角色模型：{role}")
        model.role = role
        return model


# ---------------------------------------------------------------- REST 助手


def unwrap(response) -> dict:
    """成功信封断言（契约 rest.py docstring）并返回 data。"""
    assert response.status_code < 300, response.text
    body = response.json()
    assert set(body) == {"code", "data", "message"}
    assert body["code"] == SUCCESS_CODE
    return body["data"]


async def create_session(client: AsyncClient, **payload: object) -> SessionDetail:
    response = await client.post(SESSIONS, json=payload)
    assert response.status_code == 201, response.text
    return SessionDetail.model_validate(unwrap(response))


async def post_message(client: AsyncClient, session_id: str, content: str):
    return await client.post(f"{SESSIONS}/{session_id}/messages", json={"content": content})


async def run_state_of(client: AsyncClient, session_id: str) -> str:
    detail = SessionDetail.model_validate(unwrap(await client.get(f"{SESSIONS}/{session_id}")))
    return detail.run_state


async def wait_run_state(
    client: AsyncClient, session_id: str, expected: str, *, timeout_s: float = 15.0
) -> None:
    """轮询 run_state（REST 投影）：waiting_approval / running / idle。"""
    deadline = time.monotonic() + timeout_s
    seen = "unknown"
    while time.monotonic() < deadline:
        seen = await run_state_of(client, session_id)
        if seen == expected:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"run_state 未在 {timeout_s}s 内变为 {expected}（最后看到 {seen}）")


async def wait_for_approvals(
    db: AsyncSession, session_id: str, *, count: int, timeout_s: float = 15.0
) -> list[Approval]:
    """轮询审批行数量（background 短事务提交后可见）。"""
    deadline = time.monotonic() + timeout_s
    rows: list[Approval] = []
    while time.monotonic() < deadline:
        rows = await approvals_of(db, session_id)
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.02)
    pytest.fail(f"审批行未在 {timeout_s}s 内达到 {count} 条（实际 {len(rows)}）")


async def wait_for_events(
    db: AsyncSession,
    session_id: str,
    event_type: str,
    *,
    count: int = 1,
    timeout_s: float = 15.0,
) -> list[EventRow]:
    """轮询事件行数量（**停车完成**的判据：run 行/审批行都在事件之前落库）。"""
    deadline = time.monotonic() + timeout_s
    matched: list[EventRow] = []
    while time.monotonic() < deadline:
        rows = await events_of(db, session_id)
        matched = [row for row in rows if row.type == event_type]
        if len(matched) >= count:
            return rows
        await asyncio.sleep(0.02)
    pytest.fail(f"{event_type} 事件未在 {timeout_s}s 内达到 {count} 条（实际 {len(matched)}）")


# ---------------------------------------------------------------- 库查询


async def latest_run(db: AsyncSession, session_id: str) -> RunRow:
    row = await db.scalar(
        select(RunRow)
        .where(RunRow.session_id == uuid.UUID(session_id))
        .order_by(RunRow.started_at.desc(), RunRow.id.desc())
        .limit(1)
    )
    assert row is not None
    return row


async def approvals_of(db: AsyncSession, session_id: str) -> list[Approval]:
    rows = await db.scalars(
        select(Approval)
        .where(Approval.session_id == uuid.UUID(session_id))
        .order_by(Approval.created_at.asc(), Approval.id.asc())
    )
    return list(rows)


async def events_of(db: AsyncSession, session_id: str) -> list[EventRow]:
    rows = await db.scalars(
        select(EventRow).where(EventRow.session_id == uuid.UUID(session_id)).order_by(EventRow.seq)
    )
    return list(rows)


async def model_calls_of(db: AsyncSession, run_id: uuid.UUID) -> list[ModelCall]:
    rows = await db.scalars(
        select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.created_at)
    )
    return list(rows)


async def assistant_messages_of(db: AsyncSession, session_id: str) -> list[MessageRow]:
    rows = await db.scalars(
        select(MessageRow)
        .where(MessageRow.session_id == uuid.UUID(session_id), MessageRow.role == "assistant")
        .order_by(MessageRow.created_at)
    )
    return list(rows)


def event_payload(rows: list[EventRow], event_type: str) -> list[dict]:
    return [row.data for row in rows if row.type == event_type]


__all__ = [
    "APPROVALS",
    "SESSIONS",
    "ApprovalEnv",
    "approvals_of",
    "assistant_messages_of",
    "create_session",
    "event_payload",
    "events_of",
    "latest_run",
    "model_calls_of",
    "post_message",
    "run_state_of",
    "unwrap",
    "wait_for_approvals",
    "wait_for_events",
    "wait_run_state",
]
