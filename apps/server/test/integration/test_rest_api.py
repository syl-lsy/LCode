"""integration 层：REST 端点（test-plan-m2 §5 T6；A3 §7/§8）。

覆盖面（会话车道 + 流式接口车道）：

- **成功侧信封**：`/api/` 响应恒为 `{"code":"0","data":<DTO>,"message":"ok"}`（契约
  rest.py 模块 docstring），且 `data` 能过契约 DTO 回验（`extra="forbid"` 是机器锚点）；
- **错误侧信封**：`{"code": <RestErrorCode>, "message": ...}` 终态、不被成功信封二次包装；
  两条校验路径都覆盖——契约 DTO 结构校验（RequestValidationError → INVALID_ARGUMENT）
  与语义级校验（service 层 → MODEL_NOT_FOUND / INVALID_ARGUMENT / SESSION_NOT_FOUND）；
- **AC-004-3** 列表按最近活跃倒序 + 标题子串搜索（total 为过滤后总数）；
- **AC-014-2/3** 模型/档位热切换（PATCH 写回会话行）与非 reasoning 模型档位回落 "off"；
- **run_state** 三态由 runs 表推导（AC-006-5 刷新恢复的关键状态）；
- **AC-002-6** sandbox_provider 创建时固化当前配置值（改配置只影响新会话）；
- **AC-001-1/4** 发消息 ack + 空白拒收（MESSAGE_EMPTY）+ RUN_ALREADY_ACTIVE（AC-001 P0 串行）；
- **AC-001-2** stop 幂等收敛：message_finished(cancelled) + run_status(stopped) 事件落库；
- **AC-003-3** SSE 断线续传：Last-Event-ID 头优先于 after_seq，重放只发 `seq > 水位`；
- **AC-002-3** DELETE 会话联动 kill 沙箱 + 业务行清理；
- **AC-015-1/2/3/4** 成本：run 收口落 model_calls、三维汇总、会话成本、沙箱状态费用估算。

替身边界（A3 §9.2）：运行时三依赖经 `agent_env` 覆盖——沙箱=FakeSandbox（真 tmp + 真子进程）、
checkpointer=InMemorySaver、模型=ScriptedChatModel；事件管线/发射器/库用真件（真实 DB 短事务）。

库依赖：requires_db（conftest 重建 DB_NAME 指向的测试库）。本车道按并发隔离约定以
`DB_NAME=lcode_api_test` 运行；未配置/不可达时整层显式 skip（不红、不静默）。
"""

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from loguru import logger
from sqlalchemy import func, select

from app.core.config import sandbox_settings, sse_settings
from app.dependencies import (
    get_checkpointer,
    get_event_pipeline,
    get_model_factory,
    get_sandbox_manager,
)
from app.main import app
from app.models import Event as EventRow
from app.models import Message as MessageRow
from app.models import ModelCall, Run
from app.models import Session as SessionRow
from app.sandbox.manager import SandboxManager
from lcode_contracts.common import Usage
from lcode_contracts.errors import SUCCESS_CODE, ErrorBody
from lcode_contracts.rest import (
    CostSummary,
    MessagePage,
    MessageSendAck,
    SandboxStatus,
    SessionCost,
    SessionDetail,
    SessionPage,
    StopRunAck,
)
from test.support import scripts
from test.support.fake_llm import ScriptedChatModel
from test.support.fake_sandbox import FakeSandbox

pytestmark = pytest.mark.requires_db

SESSIONS = "/api/v1/sessions"


def _unwrap(response) -> dict:
    """成功信封断言（契约 rest.py docstring）并返回 data。"""
    assert response.status_code < 300, response.text
    body = response.json()
    assert set(body) == {"code", "data", "message"}
    assert body["code"] == SUCCESS_CODE
    assert body["message"] == "ok"
    return body["data"]


def _error(response, code: str) -> ErrorBody:
    """错误信封断言：`{code, message}` 终态（不被成功信封包装），code 为契约码。"""
    body = response.json()
    assert set(body) == {"code", "message"}, body
    error = ErrorBody.model_validate(body)
    assert error.code == code
    assert error.message
    return error


async def _create(client, **payload) -> SessionDetail:
    """POST /sessions → 契约 DTO（201 = 资源创建，信封 middleware 保留状态码）。"""
    response = await client.post(SESSIONS, json=payload)
    assert response.status_code == 201, response.text
    return SessionDetail.model_validate(_unwrap(response))


async def _patch(client, session_id: str, **payload) -> SessionDetail:
    response = await client.patch(f"{SESSIONS}/{session_id}", json=payload)
    assert response.status_code == 200, response.text
    return SessionDetail.model_validate(_unwrap(response))


# ---------------------------------------------------------------- POST /sessions


async def test_create_session_returns_envelope_and_contract_dto(async_client) -> None:
    """默认会话：契约 DTO 逐字段落地（角色级默认模型/档位、审批模式、沙箱 provider）。"""
    detail = await _create(async_client)

    uuid.UUID(detail.id)  # 契约 ID 约定：服务端生成 UUID 字符串
    assert detail.title == "新会话"
    assert detail.status == "active"
    assert detail.model == "qwen3.7-plus"  # gateway.default_model
    assert detail.reasoning_effort == "high"  # roles.main 档位（优先级链的角色级）
    assert detail.approval_mode == "manual"
    assert detail.sandbox_provider == sandbox_settings.provider  # 创建时固化当前配置
    assert detail.run_state == "idle"
    # timestamptz 往返：wire 层带时区（契约 TIMESTAMP_FIELD 约定）
    assert detail.created_at.tzinfo is not None
    assert detail.created_at <= detail.last_active_at


async def test_create_session_persists_explicit_options(async_client) -> None:
    detail = await _create(
        async_client,
        title="重构沙箱适配层",
        model="qwen3.8-flash",
        reasoning_effort="low",
        approval_mode="auto",
    )

    assert detail.title == "重构沙箱适配层"
    assert detail.model == "qwen3.8-flash"
    assert detail.reasoning_effort == "low"
    assert detail.approval_mode == "auto"


async def test_create_session_freezes_sandbox_provider_from_config(
    async_client, monkeypatch
) -> None:
    """AC-002-6：provider 是创建期快照——改全局配置只影响新会话（旧会话列不动）。"""
    existing = await _create(async_client)

    monkeypatch.setattr(sandbox_settings, "provider", "opensandbox")
    created = await _create(async_client)

    assert created.sandbox_provider == "opensandbox"
    assert existing.sandbox_provider == "bailian"  # 已存在会话不被配置变更回溯改写


async def test_create_session_with_unknown_model_is_model_not_found(async_client) -> None:
    response = await async_client.post(SESSIONS, json={"model": "gpt-不存在"})

    assert response.status_code == 404
    _error(response, "MODEL_NOT_FOUND")


async def test_create_session_non_reasoning_model_falls_back_to_off(async_client) -> None:
    """AC-014-3 后端等价行为：模型不支持 reasoning 时未显式给档位 → 落 "off"。"""
    detail = await _create(async_client, model="qwen3.6-flash")

    assert (detail.model, detail.reasoning_effort) == ("qwen3.6-flash", "off")


async def test_create_session_rejects_explicit_effort_on_non_reasoning_model(async_client) -> None:
    response = await async_client.post(
        SESSIONS, json={"model": "qwen3.6-flash", "reasoning_effort": "high"}
    )

    assert response.status_code == 422
    _error(response, "INVALID_ARGUMENT")


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature": 0.7},  # AC-014-1：UI/契约都不暴露采样参数（extra="forbid" 硬拒）
        {"reasoning_effort": "ultra"},  # 档位值域外（契约 EffortLevel 字面量）
        {"approval_mode": "always"},  # 审批模式值域外
    ],
)
async def test_create_session_rejects_payload_outside_contract(async_client, payload) -> None:
    """契约 DTO 是入参唯一闸门：结构/值域非法一律 422 INVALID_ARGUMENT。"""
    response = await async_client.post(SESSIONS, json=payload)

    assert response.status_code == 422
    _error(response, "INVALID_ARGUMENT")


# ---------------------------------------------------------------- GET /sessions


async def test_list_sessions_sorted_by_activity_and_title_searchable(
    async_client, db_session
) -> None:
    """AC-004-3：按 last_active_at 倒序；?title= 子串过滤且 total 为过滤后总数。"""
    titles = ["重构沙箱适配层", "沙箱排错记录", "文档整理"]
    created = {title: await _create(async_client, title=title) for title in titles}

    # 活跃顺序 = 创建顺序的逆序（显式写入，避免依赖 now() 的分辨率）
    activity_order = ["文档整理", "沙箱排错记录", "重构沙箱适配层"]
    base = datetime.now(UTC)
    for offset, title in enumerate(activity_order):
        row = await db_session.get(SessionRow, uuid.UUID(created[title].id))
        assert row is not None
        row.last_active_at = base - timedelta(minutes=offset)
    await db_session.commit()

    page = SessionPage.model_validate(_unwrap(await async_client.get(SESSIONS)))
    assert page.total == 3
    assert [item.title for item in page.items] == activity_order

    filtered = SessionPage.model_validate(
        _unwrap(await async_client.get(SESSIONS, params={"title": "沙箱"}))
    )
    assert filtered.total == 2  # 过滤后总数（不是全表 total）
    assert [item.title for item in filtered.items] == ["沙箱排错记录", "重构沙箱适配层"]

    empty = SessionPage.model_validate(
        _unwrap(await async_client.get(SESSIONS, params={"title": "不存在的标题"}))
    )
    assert (empty.total, empty.items) == (0, [])


# ---------------------------------------------------------------- GET /sessions/{id}


async def test_get_session_detail_matches_created_row(async_client) -> None:
    created = await _create(async_client, title="详情回读")

    detail = SessionDetail.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}"))
    )

    assert detail == created


@pytest.mark.parametrize("session_id", ["0f7b2c6e-0000-4000-8000-000000000000", "not-a-uuid"])
async def test_get_session_detail_is_session_not_found(async_client, session_id) -> None:
    """未知 id 与非法 id 同口径：会话不存在（契约该端点只声明 SESSION_NOT_FOUND）。"""
    response = await async_client.get(f"{SESSIONS}/{session_id}")

    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


# ---------------------------------------------------------------- PATCH /sessions/{id}


async def test_patch_session_hot_switch_model_and_effort(async_client) -> None:
    """AC-014-2：模型/档位写回会话行（run 启动时重读 → 下一条消息起生效）。"""
    created = await _create(async_client)
    assert created.reasoning_effort == "high"

    # 新模型同样支持 high → 未显式给档位时保留现档位
    switched = await _patch(async_client, created.id, model="qwen3.8-flash")
    assert (switched.model, switched.reasoning_effort) == ("qwen3.8-flash", "high")
    assert switched.updated_at >= created.updated_at

    # AC-014-3：切到不支持 reasoning 的模型且未显式给档位 → 回落 "off"
    downgraded = await _patch(async_client, created.id, model="qwen3.6-flash")
    assert (downgraded.model, downgraded.reasoning_effort) == ("qwen3.6-flash", "off")

    # 显式档位 + 支持档位的模型 → 按请求落库
    restored = await _patch(
        async_client, created.id, model="qwen3.8-flash", reasoning_effort="medium"
    )
    assert (restored.model, restored.reasoning_effort) == ("qwen3.8-flash", "medium")


async def test_patch_session_updates_metadata(async_client) -> None:
    created = await _create(async_client)

    patched = await _patch(async_client, created.id, title="重命名后的会话", approval_mode="auto")

    assert patched.title == "重命名后的会话"
    assert patched.approval_mode == "auto"
    assert patched.model == created.model  # 未给字段不动
    assert patched.reasoning_effort == created.reasoning_effort


async def test_patch_session_rejects_effort_unsupported_by_current_model(async_client) -> None:
    created = await _create(async_client, model="qwen3.6-flash")

    response = await async_client.patch(
        f"{SESSIONS}/{created.id}", json={"reasoning_effort": "low"}
    )

    assert response.status_code == 422
    _error(response, "INVALID_ARGUMENT")


async def test_patch_session_error_paths(async_client) -> None:
    created = await _create(async_client)

    empty = await async_client.patch(f"{SESSIONS}/{created.id}", json={})
    assert empty.status_code == 422  # SessionPatch 至少提供一个更新字段
    _error(empty, "INVALID_ARGUMENT")

    unknown_model = await async_client.patch(
        f"{SESSIONS}/{created.id}", json={"model": "gpt-不存在"}
    )
    assert unknown_model.status_code == 404
    _error(unknown_model, "MODEL_NOT_FOUND")

    extra_field = await async_client.patch(f"{SESSIONS}/{created.id}", json={"top_k": 40})
    assert extra_field.status_code == 422
    _error(extra_field, "INVALID_ARGUMENT")

    missing = await async_client.patch(f"{SESSIONS}/{uuid.uuid4()}", json={"title": "改不到的行"})
    assert missing.status_code == 404
    _error(missing, "SESSION_NOT_FOUND")


# ---------------------------------------------------------------- run_state 推导


async def test_run_state_derived_from_runs_table(async_client, db_session) -> None:
    """run_state 三态由 runs 表推导（AC-006-5：waiting_approval 可跨刷新续操作）。

    - 无活动 run（含只有终态 run）→ idle；
    - 最新活动 run running → running；waiting_approval → waiting_approval。
    """
    idle = await _create(async_client, title="空闲会话")
    running = await _create(async_client, title="运行中会话")
    waiting = await _create(async_client, title="等待审批会话")

    base = datetime.now(UTC)
    db_session.add_all(
        [
            Run(session_id=uuid.UUID(idle.id), status="completed", started_at=base),
            Run(session_id=uuid.UUID(running.id), status="running", started_at=base),
            # 先完成再挂起：最新活动 run 优先（started_at 更晚）
            Run(session_id=uuid.UUID(waiting.id), status="completed", started_at=base),
            Run(
                session_id=uuid.UUID(waiting.id),
                status="waiting_approval",
                started_at=base + timedelta(minutes=1),
            ),
        ]
    )
    await db_session.commit()

    details = {
        title: SessionDetail.model_validate(
            _unwrap(await async_client.get(f"{SESSIONS}/{session_id}"))
        )
        for title, session_id in (
            ("空闲会话", idle.id),
            ("运行中会话", running.id),
            ("等待审批会话", waiting.id),
        )
    }
    assert details["空闲会话"].run_state == "idle"
    assert details["运行中会话"].run_state == "running"
    assert details["等待审批会话"].run_state == "waiting_approval"

    # 列表项同样携带 run_state（SessionDetail 投影，AC-006-5 首屏恢复）
    page = SessionPage.model_validate(_unwrap(await async_client.get(SESSIONS)))
    states = {item.title: item.run_state for item in page.items}
    assert states == {
        "空闲会话": "idle",
        "运行中会话": "running",
        "等待审批会话": "waiting_approval",
    }


# ---------------------------------------------------------------- 运行时替身（本车道专属）


class _AgentEnv:
    """运行时三依赖的替身集合：沙箱管理器 / checkpointer / 模型工厂。

    事件管线（`get_event_pipeline`）**不覆盖**——走真件（真 DB 短事务 + 真 seq 分配），
    这正是本车道要验证的「业务行 commit → emit → ack」顺序与事件重放语义。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.sandboxes: dict[str, FakeSandbox] = {}
        """会话 id → 最近一次创建的假沙箱（断言 kill/create 调用轨迹用）。"""

        def _factory(provider: str, session_id: str) -> FakeSandbox:  # noqa: ARG001 —— provider 由用例固化
            sandbox = FakeSandbox(root=tmp_path / session_id, session_id=session_id)
            self.sandboxes[session_id] = sandbox
            return sandbox

        self.manager = SandboxManager(factory=_factory)
        self.checkpointer = InMemorySaver()
        # 事件管线不覆盖（get_event_pipeline 走真件）：测试需要其总线做 SSE 收尾与断言
        self.pipeline = get_event_pipeline()
        self._model: ScriptedChatModel | None = None

    def set_model(self, model: ScriptedChatModel) -> None:
        """按用例换剧本（默认直答 SIMPLE_REPLY）。"""
        self._model = model

    def model_factory(self, role: str) -> ScriptedChatModel:
        if self._model is None:
            self._model = ScriptedChatModel(steps=scripts.simple_reply())
        self._model.role = role
        return self._model


@pytest.fixture
def agent_env(async_client, tmp_path: Path):
    env = _AgentEnv(tmp_path)
    app.dependency_overrides[get_sandbox_manager] = lambda: env.manager
    app.dependency_overrides[get_checkpointer] = lambda: env.checkpointer
    # 依赖本身必须零参（FastAPI 会内省依赖签名）：返回工厂而不是当工厂用
    app.dependency_overrides[get_model_factory] = lambda: env.model_factory
    yield env


async def _post_message(client, session_id: str, content: str):
    return await client.post(f"{SESSIONS}/{session_id}/messages", json={"content": content})


async def _wait_run_idle(client, session_id: str, timeout_s: float = 15.0) -> None:
    """轮询直到 run 收敛为终态（后台任务用独立短事务写行，轮询走 API 的读快照）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        detail = SessionDetail.model_validate(_unwrap(await client.get(f"{SESSIONS}/{session_id}")))
        if detail.run_state == "idle":
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"run 未在 {timeout_s}s 内收敛为 idle（session={session_id}）")


def _parse_frame(frame: str) -> dict:
    """SSE 帧 → 字段字典（`id`/`event`/`data`；注释行跳过）。"""
    fields: dict[str, str] = {}
    for line in frame.strip("\n").splitlines():
        if line.startswith(":"):
            continue
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


async def _read_sse(
    client,
    bus,
    session_id: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout_s: float = 10.0,
) -> list[str]:
    """采集一次 SSE 连接的完整帧序列（httpx ASGITransport 会缓冲整个响应体）。

    SSE 的实时段是无限流，测试按契约的**订阅关闭路径**收尾：等端点建立订阅（总线上
    出现订阅者）后 `bus.close_all()`——帧流随即结束、连接完成，缓冲体一次性可读。
    这与「订阅被关闭（慢消费者/关停）→ 客户端带 Last-Event-ID 重连」是同一条生产路径。
    """
    request = client.build_request("GET", url, params=params, headers=headers)
    send_task = asyncio.create_task(client.send(request, stream=True))
    deadline = time.monotonic() + timeout_s
    while bus.subscriber_count(session_id) == 0:
        if time.monotonic() > deadline:
            pytest.fail("SSE 端点未在时限内建立订阅")
        await asyncio.sleep(0.01)
    bus.close_all("test-collect-done")  # 收尾实时段 → 帧流自然结束
    response = await send_task
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    body = (await response.aread()).decode("utf-8")
    await response.aclose()
    return [frame for frame in body.split("\n\n") if frame]


# ---------------------------------------------------------------- POST /sessions/{id}/messages


async def test_post_message_returns_send_ack_and_persists_facts(
    async_client, agent_env, db_session
) -> None:
    """AC-001-1：ack 携带水位；业务行（消息+run）先 commit，message_created 事件落库。"""
    created = await _create(async_client, title="发送消息")
    response = await _post_message(async_client, created.id, scripts.SIMPLE_USER_TEXT)

    assert response.status_code == 200, response.text
    ack = MessageSendAck.model_validate(_unwrap(response))
    uuid.UUID(ack.message_id)
    uuid.UUID(ack.run_id)
    assert ack.event_seq >= 0

    # REST 来源事件顺序（A3 §6.1）：业务行落库→commit→emit→ack —— ack 返回时行已可查
    row = await db_session.get(MessageRow, uuid.UUID(ack.message_id))
    assert row is not None
    assert (row.role, row.content, row.session_id) == (
        "user",
        scripts.SIMPLE_USER_TEXT,
        uuid.UUID(created.id),
    )
    run_row = await db_session.get(Run, uuid.UUID(ack.run_id))
    assert run_row is not None and run_row.session_id == uuid.UUID(created.id)

    # message_created 已落库且占用水位 seq（客户端以 after_seq=event_seq 订阅，落差由重放补齐）
    first = await db_session.scalar(
        select(EventRow)
        .where(EventRow.session_id == uuid.UUID(created.id))
        .order_by(EventRow.seq)
        .limit(1)
    )
    assert first is not None
    assert first.type == "message_created"
    assert first.seq == ack.event_seq

    # run 收敛后：assistant 消息落库（历史持久化，AC-004-1）
    await _wait_run_idle(async_client, created.id)
    page = MessagePage.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/messages"))
    )
    assert {item.role for item in page.items} == {"user", "assistant"}
    assistant = next(item for item in page.items if item.role == "assistant")
    assert scripts.SIMPLE_REPLY_TEXT in assistant.content
    assert assistant.finish_reason == "stop"
    assert assistant.run_id == ack.run_id


async def test_message_blank_returns_message_empty(async_client, agent_env) -> None:
    """AC-001-4：空白消息 → 422 MESSAGE_EMPTY（service 层首位前置检查，契约区分度）。"""
    created = await _create(async_client, title="空白消息")

    for blank in ("", "   ", "\n\t "):
        response = await _post_message(async_client, created.id, blank)
        assert response.status_code == 422, response.text
        _error(response, "MESSAGE_EMPTY")


async def test_message_rejects_payload_outside_contract(async_client, agent_env) -> None:
    """契约 DTO 仍是结构闸门：私货字段（采样参数）与非字符串 content 一律 422。"""
    created = await _create(async_client, title="私货字段")

    extra = await _post_message_client_with_temperature(async_client, created.id)
    assert extra.status_code == 422
    _error(extra, "INVALID_ARGUMENT")

    wrong_type = await async_client.post(f"{SESSIONS}/{created.id}/messages", json={"content": 123})
    assert wrong_type.status_code == 422
    _error(wrong_type, "INVALID_ARGUMENT")


async def _post_message_client_with_temperature(client, session_id: str):
    """发一条带采样参数的消息（AC-014-1：契约不暴露采样参数，extra=forbid 硬拒）。"""
    return await client.post(
        f"{SESSIONS}/{session_id}/messages", json={"content": "你好", "temperature": 0.7}
    )


async def test_post_message_unknown_session_is_session_not_found(async_client, agent_env) -> None:
    response = await _post_message(async_client, str(uuid.uuid4()), "你好")

    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


@pytest.mark.parametrize("active_status", ["running", "waiting_approval"])
async def test_post_message_while_run_active_is_run_already_active(
    async_client, agent_env, db_session, active_status
) -> None:
    """同会话已有活动 run 再发消息 → 409（P0 会话级串行，契约端点 errors 声明）。"""
    created = await _create(async_client, title=f"串行-{active_status}")
    db_session.add(
        Run(
            session_id=uuid.UUID(created.id),
            status=active_status,
            started_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    response = await _post_message(async_client, created.id, "你好")

    assert response.status_code == 409
    _error(response, "RUN_ALREADY_ACTIVE")


# ---------------------------------------------------------------- GET /sessions/{id}/messages


async def test_history_pagination_cursor(async_client, agent_env, monkeypatch) -> None:
    """消息历史游标分页：键集分页不重不漏，next_cursor 为空表示尾页（契约 rest.py）。"""
    from app.services import message_service

    monkeypatch.setattr(message_service, "MESSAGE_PAGE_SIZE", 2)
    created = await _create(async_client, title="历史分页")
    for index in range(3):  # 3 轮对话
        await _post_message(async_client, created.id, f"第{index}条")
        await _wait_run_idle(async_client, created.id)

    url = f"{SESSIONS}/{created.id}/messages"
    # 注：每轮对话只产生 1 条 assistant 收口（translator 已按上游 id 去重，并合并 tap
    # 附言/收口——见 run_service 模块 docstring），3 轮 = 3 user + 3 assistant = 6 行。
    expected_sizes = [2, 2, 2]  # 6 行 ÷ 页大小 2
    pages: list[MessagePage] = []
    cursor: str | None = None
    for index, expected in enumerate(expected_sizes):
        params = {"cursor": cursor} if cursor else None
        page = MessagePage.model_validate(_unwrap(await async_client.get(url, params=params)))
        assert len(page.items) == expected, f"第{index + 1}页行数不符"
        pages.append(page)
        cursor = page.next_cursor
    assert cursor is None, "尾页 next_cursor 必须为空（契约 rest.py 分页约定）"

    ids = [item.id for page in pages for item in page.items]
    assert len(ids) == len(set(ids)) == 6  # 不重不漏

    # 排序：最新在前（键集分页方向 = created_at 倒序），最新一条是第 2 轮的 assistant 回复
    assert pages[0].items[0].role == "assistant"
    assert "第2条" not in pages[0].items[0].content


async def test_history_unknown_session_is_session_not_found(async_client, agent_env) -> None:
    response = await async_client.get(f"{SESSIONS}/{uuid.uuid4()}/messages")

    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


# ---------------------------------------------------------------- POST /runs/{run_id}/stop


async def test_stop_run_idempotent_and_emits_cancelled(async_client, agent_env, db_session) -> None:
    """AC-001-2：stop 返回即收敛（run 行终态 + cancelled/stopped 事件落库）；重复 stop 幂等。"""
    created = await _create(async_client, title="停止运行")
    agent_env.set_model(
        ScriptedChatModel(steps=scripts.stop_stream(), chunk_size=8, delay_seconds=0.01)
    )
    response = await _post_message(async_client, created.id, "开始长输出")
    ack = MessageSendAck.model_validate(_unwrap(response))

    # 等首个文本增量真正落库（流已开始产出）再停——否则在飞消息不存在，cancelled 收口
    # 无从谈起（AC-001-2 断言的是「已生成内容保留」，零内容时无 message_finished 是正确行为）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        delta = await db_session.scalar(
            select(EventRow.id)
            .where(
                EventRow.session_id == uuid.UUID(created.id),
                EventRow.type == "message_delta",
            )
            .limit(1)
        )
        if delta is not None:
            break
        await asyncio.sleep(0.01)
    assert delta is not None, "5s 内未见到任何 message_delta（假模型流未启动）"

    first_stop = await async_client.post(f"/api/v1/runs/{ack.run_id}/stop")
    assert first_stop.status_code == 200, first_stop.text
    body = StopRunAck.model_validate(_unwrap(first_stop))
    assert body.status == "stopped"  # 契约：status 恒 stopped

    # 停止诚实收敛：stop 返回时 run 行已是终态（不返回「已请求停止」的中间态）
    run_row = await db_session.get(Run, uuid.UUID(ack.run_id))
    assert run_row is not None
    await db_session.refresh(run_row)
    assert run_row.status == "stopped"
    assert run_row.finished_at is not None

    # 事件侧：message_finished(cancelled) + run_status(stopped) 都已落库（AC-001-2 保留已生成）
    rows = list(
        await db_session.scalars(
            select(EventRow)
            .where(EventRow.session_id == uuid.UUID(created.id))
            .order_by(EventRow.seq)
        )
    )
    types = {row.type for row in rows}
    assert "run_status" in types and "message_created" in types
    finished = [row for row in rows if row.type == "message_finished"]
    assert finished, "停止必须补发 in-flight 消息的 message_finished"
    assert all(row.data.get("finish_reason") == "cancelled" for row in finished)
    run_stopped = [
        row for row in rows if row.type == "run_status" and row.data.get("phase") == "stopped"
    ]
    assert run_stopped, "停止必须发 run_status(stopped)"

    # 已生成的 assistant 增量保留：凡有 assistant 消息行，finish_reason 恒 cancelled
    page = MessagePage.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/messages"))
    )
    for item in page.items:
        if item.role == "assistant":
            assert item.finish_reason == "cancelled"

    # 幂等：对已停止 run 重复 stop → 200 + stopped（契约 StopRunAck 注释）
    second_stop = await async_client.post(f"/api/v1/runs/{ack.run_id}/stop")
    assert second_stop.status_code == 200
    assert StopRunAck.model_validate(_unwrap(second_stop)).status == "stopped"

    # 收尾沉降：任务取消会打断在途的 asyncpg 协议操作（连接归还/清理是异步的），
    # 让清理在本测的循环内完成，避免旧循环的连接对象串进下一个测试（不同 loop 崩溃）。
    await asyncio.sleep(0.3)


async def test_stop_run_errors(async_client, agent_env, db_session) -> None:
    """stop 错误路径：未知 run → 404 RUN_NOT_FOUND；终态（completed/error）→ 409 RUN_NOT_ACTIVE。"""
    missing = await async_client.post(f"/api/v1/runs/{uuid.uuid4()}/stop")
    assert missing.status_code == 404
    _error(missing, "RUN_NOT_FOUND")

    created = await _create(async_client, title="终态运行")
    db_session.add(
        Run(
            session_id=uuid.UUID(created.id),
            status="completed",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    response = await async_client.post(f"/api/v1/runs/{uuid.uuid4()}/stop")
    assert response.status_code == 404  # 路径里的是未知 run

    db_session.add(
        Run(
            session_id=uuid.UUID(created.id),
            status="error",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    errored_run = await db_session.scalar(
        select(Run).where(Run.session_id == uuid.UUID(created.id), Run.status == "error")
    )
    assert errored_run is not None
    response = await async_client.post(f"/api/v1/runs/{errored_run.id}/stop")
    assert response.status_code == 409
    _error(response, "RUN_NOT_ACTIVE")


async def test_finalize_sink_failure_logs_error_and_still_persists(
    async_client, agent_env, db_session, monkeypatch
) -> None:
    """F1b：收口事件发射失败（注入失败 sink）→ ERROR 日志留痕，且 run 行终态与
    model_calls 照常落库（事件可回放重建，成本/审计行不得因 sink 故障丢行）。"""
    from app.services import run_service as run_service_module

    async def _boom(_self: Any, _phase: Any, **_kwargs: Any) -> None:
        raise RuntimeError("sink 收口崩溃（F1b 测试注入）")

    monkeypatch.setattr(run_service_module._RunEventSink, "finish", _boom)

    records: list[Any] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    try:
        created = await _create(async_client, title="sink 故障收口")
        await _post_message(async_client, created.id, scripts.SIMPLE_USER_TEXT)
        await _wait_run_idle(async_client, created.id)
    finally:
        with contextlib.suppress(ValueError):
            logger.remove(sink_id)

    errors = [
        record
        for record in records
        if record["level"].name == "ERROR"
        and record["extra"].get("action") == "收口运行"
        and record["extra"].get("entity") == "run"
    ]
    assert errors, "sink 收口失败必须留 ERROR 日志（不允许无日志 suppress）"
    assert errors[0]["exception"] is not None

    run_rows = list(
        await db_session.scalars(select(Run).where(Run.session_id == uuid.UUID(created.id)))
    )
    assert run_rows, "run 行必须存在"
    assert all(row.status == "completed" for row in run_rows), "run 行必须收敛终态（completed）"

    call_row = await db_session.scalar(
        select(ModelCall).where(ModelCall.session_id == uuid.UUID(created.id))
    )
    assert call_row is not None, "model_calls 不得因 sink 故障丢行"
    assert (call_row.input_tokens, call_row.output_tokens) == (12, 7)


async def test_stop_no_handle_race_run_converged_to_error_returns_run_not_active(
    async_client, agent_env, db_session, monkeypatch
) -> None:
    """F12：无句柄 stop 的并发窗口——首读行活动、兜底迁移前另一写家已把行标为 error →
    必须 409 RUN_NOT_ACTIVE，且不得补发 run_status(stopped)（误报会污染前端状态机）。"""
    from app.services import run_service as run_service_module

    created = await _create(async_client, title="stop 竞争-error")
    run_row = Run(session_id=uuid.UUID(created.id), status="running", started_at=datetime.now(UTC))
    db_session.add(run_row)
    await db_session.commit()
    run_id = run_row.id

    original = run_service_module.RunService._mark_run_row

    async def _racing_mark(_self: Any, mark_run_id: str, mark_status: str) -> bool:
        # 模拟并发收敛：stop 首读行后、兜底迁移前，_close_run_row 的短事务已把行标为 error
        row = await db_session.get(Run, run_id)
        assert row is not None
        row.status = "error"
        row.finished_at = datetime.now(UTC)
        await db_session.commit()
        return bool(await original(_self, mark_run_id, mark_status))

    monkeypatch.setattr(run_service_module.RunService, "_mark_run_row", _racing_mark)

    response = await async_client.post(f"/api/v1/runs/{run_id}/stop")

    assert response.status_code == 409, response.text
    _error(response, "RUN_NOT_ACTIVE")

    rows = list(
        await db_session.scalars(
            select(EventRow).where(EventRow.session_id == uuid.UUID(created.id))
        )
    )
    assert not [
        row for row in rows if row.type == "run_status" and row.data.get("phase") == "stopped"
    ], "行未被本请求迁移时不得补发 run_status(stopped)"

    # 收尾沉降：同 test_stop_run_idempotent_and_emits_cancelled 的口径（避免跨循环连接串台）
    await asyncio.sleep(0.3)


# ---------------------------------------------------------------- GET /sessions/{id}/events（SSE）


async def test_events_replay_after_seq_and_last_event_id_priority(
    async_client, agent_env, db_session
) -> None:
    """AC-003-3：重放只发 seq > 水位；Last-Event-ID 头优先于 after_seq（契约传输语义）。"""
    created = await _create(async_client, title="事件重放")
    await _post_message(async_client, created.id, scripts.SIMPLE_USER_TEXT)
    await _wait_run_idle(async_client, created.id)
    session_key = uuid.UUID(created.id)
    bus = agent_env.pipeline.bus

    last_seq = int(
        await db_session.scalar(
            select(func.max(EventRow.seq)).where(EventRow.session_id == session_key)
        )
    )
    assert last_seq >= 1  # 至少 message_created + run 收口两族事件
    url = f"{SESSIONS}/{created.id}/events"

    # 全量重放（头与参皆缺）：首帧 retry，事件帧按 seq 升序，data 为单行契约 JSON
    frames = await _read_sse(async_client, bus, created.id, url)
    assert frames[0].strip() == f"retry: {sse_settings.retry_ms}"
    parsed = [_parse_frame(frame) for frame in frames[1:] if "id" in _parse_frame(frame)]
    seqs = [int(item["id"]) for item in parsed]
    assert seqs[0] == 0 and seqs == sorted(seqs)
    assert parsed[0]["event"] == "message_created"
    payload = json.loads(parsed[0]["data"])
    assert payload["type"] == "message_created"
    assert payload["role"] == "user"
    assert "\n" not in parsed[0]["data"]  # data 单行 JSON（JSON 转义吸收换行）

    # 显式 after_seq=0 是续传语义（客户端已收到 seq 0）→ 只补 seq ≥ 1
    frames = await _read_sse(async_client, bus, created.id, url, params={"after_seq": 0})
    seqs = [int(fields["id"]) for frame in frames[1:] if "id" in (fields := _parse_frame(frame))]
    assert seqs and seqs[0] == 1

    # after_seq=水位：只补增量（seq 严格大于水位）
    watermark = last_seq - 1
    frames = await _read_sse(async_client, bus, created.id, url, params={"after_seq": watermark})
    seqs = [int(fields["id"]) for frame in frames[1:] if "id" in (fields := _parse_frame(frame))]
    assert seqs and all(seq > watermark for seq in seqs)
    assert seqs[0] == watermark + 1

    # Last-Event-ID 头优先于 after_seq=0：带头的重放窗口与上面一致（不从 0 全量重放）
    frames = await _read_sse(
        async_client,
        bus,
        created.id,
        url,
        params={"after_seq": 0},
        headers={"Last-Event-ID": str(watermark)},
    )
    seqs = [int(fields["id"]) for frame in frames[1:] if "id" in (fields := _parse_frame(frame))]
    assert seqs and all(seq > watermark for seq in seqs)


async def test_events_unknown_session_returns_rest_error_before_stream(
    async_client, agent_env
) -> None:
    """流前校验走普通 REST 错误体（不进流、不吐 SSE 帧）。"""
    response = await async_client.get(f"{SESSIONS}/{uuid.uuid4()}/events")

    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


# ---------------------------------------------------------------- DELETE /sessions/{id}


async def test_delete_session_triggers_kill_and_removes_rows(
    async_client, agent_env, db_session
) -> None:
    """AC-002-3：DELETE 同步 kill 会话沙箱并清理该会话全部业务行。

    懒加载下 provision 由工具执行触发：用带 `ls` 工具调用的剧本跑一条消息——首工具
    调用才 create，随后 DELETE 必须 kill 已 provision 的实例。
    """
    created = await _create(async_client, title="删除会话")
    agent_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    await _post_message(async_client, created.id, "列一下目录")
    await _wait_run_idle(async_client, created.id)
    session_key = uuid.UUID(created.id)

    # 首工具调用已懒 provision：DELETE 前处于运行态
    assert agent_env.manager.stable_phase(str(session_key)) == "running"

    response = await async_client.delete(f"{SESSIONS}/{created.id}")
    assert response.status_code == 204

    # 沙箱已 kill（manager 账本相位 + FakeSandbox 调用轨迹）
    assert agent_env.manager.stable_phase(str(session_key)) == "killed"
    assert agent_env.manager.get(str(session_key)) is None
    sandbox = agent_env.sandboxes.get(str(session_key))
    assert sandbox is not None and "kill" in sandbox.calls, "DELETE 未 kill 已 provision 沙箱"

    # 业务行已清理（会话行 + 挂在其下的消息/事件/run 行）
    for model, column in (
        (SessionRow, SessionRow.id),
        (MessageRow, MessageRow.session_id),
        (EventRow, EventRow.session_id),
        (Run, Run.session_id),
    ):
        count = await db_session.scalar(
            select(func.count()).select_from(model).where(column == session_key)
        )
        assert count == 0, f"{model.__tablename__} 未随会话删除"

    # 再删 → 404
    response = await async_client.delete(f"{SESSIONS}/{created.id}")
    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


async def test_delete_session_stops_active_run_without_fk_error(
    async_client, agent_env, db_session
) -> None:
    """AC-002-3（活跃 run）：DELETE 必须按**会话 id** 找到在跑句柄并先停 run。

    注册表以 run_id 为键（stop 路径依赖该口径）；旧实现用会话 id 去查恒为 None，
    于是删除后后台 run 变孤儿：`_purge` 删掉 sessions 行，孤儿 run 的下一条事件
    emit 撞 `events.session_id → sessions.id` 外键，以 internal error 收场。
    """
    created = await _create(async_client, title="删除活跃会话")
    session_key = uuid.UUID(created.id)
    # 40 块 × 20ms ≈ 0.8s 的长流：保证 DELETE 到达时 run 仍在产出
    agent_env.set_model(
        ScriptedChatModel(steps=scripts.stop_stream(), chunk_size=8, delay_seconds=0.02)
    )

    records: list[Any] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    try:
        response = await _post_message(async_client, created.id, "开始长输出")
        ack = MessageSendAck.model_validate(_unwrap(response))

        # 等首个文本增量真正落库（run 处于活跃产出中，不是「已排队未启动」）
        deadline = time.monotonic() + 5
        delta = None
        while time.monotonic() < deadline:
            delta = await db_session.scalar(
                select(EventRow.id)
                .where(
                    EventRow.session_id == session_key,
                    EventRow.type == "message_delta",
                )
                .limit(1)
            )
            if delta is not None:
                break
            await asyncio.sleep(0.01)
        assert delta is not None, "5s 内未见到 message_delta（长流未启动）"

        # run 仍活跃时删除会话
        response = await async_client.delete(f"{SESSIONS}/{created.id}")
        assert response.status_code == 204, response.text

        # 收敛点：DELETE 返回时句柄已摘除（_drive 的 finally 在任务收口后 pop）
        from app.dependencies import _active_runs

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _active_runs.get(ack.run_id) is not None:
            await asyncio.sleep(0.01)
        assert _active_runs.get(ack.run_id) is None, "DELETE 返回后 run 任务仍未收敛"
    finally:
        with contextlib.suppress(ValueError):
            logger.remove(sink_id)

    # `_purge` 之后：该会话不再有 run / event 行（孤儿 run 无法再落任何事件）
    for model, column in ((EventRow, EventRow.session_id), (Run, Run.session_id)):
        count = await db_session.scalar(
            select(func.count()).select_from(model).where(column == session_key)
        )
        assert count == 0, f"{model.__tablename__} 未随会话删除"

    # 懒加载：纯聊天 run 从不实际使用沙箱——DELETE 走零网络摘除（无 provider 侧实例可 kill）
    sandbox = agent_env.sandboxes.get(str(session_key))
    assert sandbox is not None and sandbox.calls == [], (
        "纯聊天会话 DELETE 不应触发任何沙箱网络调用（懒加载零网络释放）"
    )
    assert agent_env.manager.stable_phase(str(session_key)) == "killed"

    # 无 ERROR：孤儿 run 在清行后继续 emit 会撞 sessions 外键（旧实现的确凿症状）
    errors = [record for record in records if record["level"].name == "ERROR"]
    assert not errors, (
        "DELETE 活跃会话后出现 ERROR 日志（孤儿 run 撞外键/FK violation）："
        f"{[record['message'] for record in errors]}"
    )

    # 收尾沉降：取消路径会打断在途 asyncpg 协议操作（同 stop 用例的跨循环口径）
    await asyncio.sleep(0.3)


async def test_delete_unknown_session_is_session_not_found(async_client, agent_env) -> None:
    response = await async_client.delete(f"{SESSIONS}/{uuid.uuid4()}")

    assert response.status_code == 404
    _error(response, "SESSION_NOT_FOUND")


# ---------------------------------------------------------------- 成本（AC-015）


async def test_model_call_recorded_after_run(async_client, agent_env, db_session) -> None:
    """AC-015-1：run 收口即逐次落 model_calls（一次调用一行；thinking token 计入 output）。"""
    created = await _create(async_client, title="成本落库")
    await _post_message(async_client, created.id, scripts.SIMPLE_USER_TEXT)
    await _wait_run_idle(async_client, created.id)

    rows = list(
        await db_session.scalars(
            select(ModelCall).where(ModelCall.session_id == uuid.UUID(created.id))
        )
    )
    assert len(rows) == 1, "单轮直答 = 一次模型调用 = 一行"
    row = rows[0]
    assert row.model == created.model
    assert row.status == "success"
    assert (row.role, row.agent_id) == ("main", None)  # 主智能体行
    assert (row.input_tokens, row.output_tokens) == (12, 7)  # 剧本 simple_reply 的 usage
    assert type(row.cost) is Decimal  # F8：numeric(12,6) 列读回即 Decimal（注解口径一致）
    assert row.total_tokens == row.input_tokens + row.output_tokens
    assert row.run_id is not None
    assert row.latency_ms is not None and row.latency_ms >= 0
    assert row.created_at is not None


async def test_tool_loop_records_one_row_per_model_call(
    async_client, agent_env, db_session
) -> None:
    """AC-015-1 逐次口径：工具循环 3 次模型调用 → 3 行；token 合计 = run 终态用量。

    M3 起写类工具要审批，本用例用只读工具循环（write_todos → ls）避免挂起。
    """
    created = await _create(async_client, title="工具循环成本")
    agent_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    await _post_message(async_client, created.id, "跑一遍工具循环")
    await _wait_run_idle(async_client, created.id)

    rows = list(
        await db_session.scalars(
            select(ModelCall)
            .where(ModelCall.session_id == uuid.UUID(created.id))
            .order_by(ModelCall.created_at)
        )
    )
    assert len(rows) == 3, "write_todos / ls / 终答 = 三次模型调用"
    assert [row.role for row in rows] == ["main", "main", "main"]
    assert [row.agent_id for row in rows] == [None, None, None]
    assert [row.status for row in rows] == ["success", "success", "success"]
    assert sum(row.input_tokens for row in rows) == 40  # 剧本收口步 usage
    assert sum(row.output_tokens for row in rows) == 15
    assert sum(row.total_tokens for row in rows) == 55


async def test_costs_summary_three_dimensions(async_client, agent_env, db_session) -> None:
    """AC-015-2：session / date / model 三维汇总（行数、token、成本求和）。"""
    first = await _create(async_client, title="成本A")
    second = await _create(async_client, title="成本B")
    today = datetime.now(UTC).replace(microsecond=0)
    yesterday = today - timedelta(days=1)
    db_session.add_all(
        [
            ModelCall(
                session_id=uuid.UUID(first.id),
                model="qwen3.7-plus",
                input_tokens=1_000_000,
                output_tokens=500_000,
                total_tokens=1_500_000,
                cost=6.0,
                status="success",
                created_at=today,
            ),
            ModelCall(
                session_id=uuid.UUID(first.id),
                model="qwen3.8-flash",
                input_tokens=1_000_000,
                output_tokens=0,
                total_tokens=1_000_000,
                cost=2.0,
                status="success",
                created_at=today,
            ),
            ModelCall(
                session_id=uuid.UUID(second.id),
                model="qwen3.7-plus",
                input_tokens=0,
                output_tokens=250_000,
                total_tokens=250_000,
                cost=2.0,
                status="success",
                created_at=yesterday,
            ),
        ]
    )
    await db_session.commit()
    url = "/api/v1/costs/summary"

    by_session = CostSummary.model_validate(
        _unwrap(await async_client.get(url, params={"dimension": "session"}))
    )
    assert by_session.dimension == "session"
    assert [(r.group_key, r.model_calls) for r in by_session.rows] == [
        (first.id, 2),
        (second.id, 1),
    ]
    assert by_session.rows[0].cost == pytest.approx(8.0)
    assert by_session.rows[0].total_tokens == 2_500_000

    by_model = CostSummary.model_validate(
        _unwrap(await async_client.get(url, params={"dimension": "model"}))
    )
    assert [(r.group_key, r.model_calls) for r in by_model.rows] == [
        ("qwen3.7-plus", 2),
        ("qwen3.8-flash", 1),
    ]
    assert [r.cost for r in by_model.rows] == pytest.approx([8.0, 2.0])

    by_date = CostSummary.model_validate(
        _unwrap(await async_client.get(url, params={"dimension": "date"}))
    )
    # 行序 = cost 降序：today 桶 8.0 > yesterday 桶 2.0
    assert [r.group_key for r in by_date.rows] == [
        today.date().isoformat(),
        yesterday.date().isoformat(),
    ]
    assert by_date.rows[0].model_calls == 2

    # 维度必填且值域受限（契约 QueryParamSpec enum + required）
    missing = await async_client.get(url)
    assert missing.status_code == 422
    _error(missing, "INVALID_ARGUMENT")
    invalid = await async_client.get(url, params={"dimension": "user"})
    assert invalid.status_code == 422
    _error(invalid, "INVALID_ARGUMENT")


async def test_session_cost_endpoint(async_client, agent_env, db_session) -> None:
    """AC-015-3：会话成本 = 模型段（token+费用）+ 沙箱段（运行/暂停估算）。"""
    created = await _create(async_client, title="会话成本")
    db_session.add(
        ModelCall(
            session_id=uuid.UUID(created.id),
            model="qwen3.7-plus",
            input_tokens=1_000_000,
            output_tokens=500_000,
            total_tokens=1_500_000,
            cost=6.0,
            status="success",
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    cost = SessionCost.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/costs"))
    )
    assert cost.model == Usage(input_tokens=1_000_000, output_tokens=500_000)
    assert cost.model_cost == pytest.approx(6.0)
    assert cost.sandbox.rate_per_hour == pytest.approx(sandbox_settings.cost_rate_per_hour)
    assert cost.sandbox.accrued >= 0.0
    assert cost.sandbox.currency == "CNY"

    missing = await async_client.get(f"{SESSIONS}/{uuid.uuid4()}/costs")
    assert missing.status_code == 404
    _error(missing, "SESSION_NOT_FOUND")


async def test_sandbox_status_endpoint(async_client, agent_env) -> None:
    """AC-002-6 / AC-015-4：沙箱状态投影（none → running）+ 费用估算字段面。"""
    created = await _create(async_client, title="沙箱状态")

    idle = SandboxStatus.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/sandbox"))
    )
    assert idle.provider == sandbox_settings.provider
    assert idle.phase == "none"  # 尚未拉起过沙箱
    assert idle.cost_estimate is not None

    await _post_message(async_client, created.id, scripts.SIMPLE_USER_TEXT)
    await _wait_run_idle(async_client, created.id)

    still_none = SandboxStatus.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/sandbox"))
    )
    # 懒加载：纯聊天 run 不 provision——状态仍 none、无 sandbox_id
    assert still_none.phase == "none"
    assert still_none.sandbox_id is None
    assert still_none.cost_estimate is not None

    # 工具执行才触发懒 provision：带 ls 工具调用的剧本跑第二条消息 → 稳态 running
    agent_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    await _post_message(async_client, created.id, "列一下目录")
    await _wait_run_idle(async_client, created.id)

    running = SandboxStatus.model_validate(
        _unwrap(await async_client.get(f"{SESSIONS}/{created.id}/sandbox"))
    )
    assert running.phase == "running"  # 稳态口径（resumed/recreated 也折叠为 running）
    assert running.sandbox_id
    assert running.cost_estimate is not None
    assert running.cost_estimate.rate_per_hour == pytest.approx(sandbox_settings.cost_rate_per_hour)

    missing = await async_client.get(f"{SESSIONS}/{uuid.uuid4()}/sandbox")
    assert missing.status_code == 404
    _error(missing, "SESSION_NOT_FOUND")
