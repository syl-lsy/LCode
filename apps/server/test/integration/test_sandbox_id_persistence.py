"""integration 层：S3 沙箱持久 id 落库与 adopt 重连（真 PG / 真事件管线 / FakeSandbox）。

验证链路（跑通**真实消息流**，不是单点函数；懒加载语义）：

1. **懒加载零网络**：纯聊天 run 全程不 provision——零沙箱调用、零计费段、不写
   `sessions.sandbox_id`（bug 修复的核心语义）；
2. **落库时点后移**：首条消息若带工具调用 → 首次实际使用（ls）才 create →
   running 生命周期事件 → `build_sandbox_event_bridges.on_lifecycle` →
   `update_session_sandbox_id_short_tx` 写 `sessions.sandbox_id`（轮询 DB 可见）；
3. **adopt 懒重连**：会话行已有持久 id（模拟上一进程生命周期）而 manager 无在册时，
   下一条消息的 run 只登记未 provision 实例；**首次工具调用**才走 adopt（不 create）；
4. **adopt 失败自愈（P1）**：持久 id 已失效 → 首工具调用 adopt 失败 → 同一次首次使用
   内降级 create 兜底（running 通知 reason="recreated"），新 id 写回会话行；后续 run
   复用已恢复实例，不重复 provision。

替身边界：模型=ScriptedChatModel（剧本驱动）、沙箱=FakeSandbox、
checkpointer=InMemorySaver；事件管线与 DB 走真件。

⚠️ `set_main_loop`：run 的懒 provision 在 to_thread 工作线程里发生命周期回调，`_launch`
跨线程投递依赖 lifespan 捕获的主循环——测试不经 lifespan，故 fixture 里显式捕获
（与生产 `main.lifespan` 同一接线，漏了它桥接发射会被安静丢弃）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.core.db import get_session_factory
from app.dependencies import (
    build_sandbox_event_bridges,
    get_checkpointer,
    get_event_pipeline,
    get_model_factory,
    get_sandbox_manager,
    set_main_loop,
)
from app.main import app
from app.models import Session as SessionRow
from app.sandbox.manager import SandboxManager
from test.support import scripts
from test.support.approval_env import (
    create_session,
    event_payload,
    events_of,
    latest_run,
    post_message,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel
from test.support.fake_sandbox import FakeSandbox

pytestmark = pytest.mark.requires_db


@pytest.fixture
async def adopt_env(async_client, db_session, tmp_path):  # noqa: ARG001 —— 依赖确保 app/DB 就绪
    """S3 落库链路专用运行时替身：**真事件桥** + FakeSandbox manager + 剧本模型。

    与 approval_env 的唯一差别：manager 挂真实 `build_sandbox_event_bridges`——
    sessions.sandbox_id 的写入方正是桥的 on_lifecycle（审批用例只看事件面，不需要它）。
    overrides 清理沿用 async_client 收尾的 clear() 约定；`flags["fail_adopt"]` 供用例
    注入「持久 id 已失效」剧本（工厂期读取，rebuild 分支的 create 不受影响）。
    """
    set_main_loop(asyncio.get_running_loop())  # 工作线程回调的跨线程投递目标（见模块 docstring）
    pipeline = get_event_pipeline()
    sandboxes: dict[str, FakeSandbox] = {}
    flags = {"fail_adopt": False}
    cell: list[SandboxManager] = []
    on_lifecycle, on_output, on_usage = build_sandbox_event_bridges(
        pipeline, lookup=lambda session_id: cell[0].get(session_id)
    )

    def factory(provider: str, session_id: str) -> FakeSandbox:
        sandbox = FakeSandbox(
            root=tmp_path / session_id, session_id=session_id, fail_adopt=flags["fail_adopt"]
        )
        sandboxes[session_id] = sandbox
        return sandbox

    manager = SandboxManager(
        factory=factory, on_lifecycle=on_lifecycle, on_output=on_output, on_usage_segment=on_usage
    )
    cell.append(manager)
    scripted: list[ScriptedChatModel] = []

    def model_factory(role: str) -> ScriptedChatModel:
        scripted[0].role = role
        return scripted[0]

    saver = InMemorySaver()
    app.dependency_overrides[get_sandbox_manager] = lambda: manager
    app.dependency_overrides[get_checkpointer] = lambda: saver
    app.dependency_overrides[get_model_factory] = lambda: model_factory

    yield SimpleNamespace(
        manager=manager, sandboxes=sandboxes, flags=flags, set_model=scripted.append
    )

    manager.shutdown()
    # 收尾沉降：桥接的后台任务（running 事件 → sessions.sandbox_id 短事务）要落完才能
    # 关事件循环——同 wired 停止测试的 0.3s 沉降口径（asyncpg 协议层取消清理由异步完成）。
    await asyncio.sleep(0.3)


async def _poll_session_sandbox_id(session_id: str, *, timeout_s: float = 10.0) -> str | None:
    """轮询会话行的 sandbox_id（桥接短事务独立提交，逐轮重读而非依赖请求会话快照）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        async with get_session_factory()() as db:
            row = await db.get(SessionRow, uuid.UUID(session_id))
        if row is not None and row.sandbox_id is not None:
            return row.sandbox_id
        await asyncio.sleep(0.02)
    return None


async def test_pure_chat_never_provisions_sandbox(adopt_env, async_client) -> None:
    """懒加载核心语义：纯聊天 run 全程零沙箱调用、零计费段、不写 sessions.sandbox_id。"""
    created = await create_session(async_client, title="纯聊天零沙箱")
    adopt_env.set_model(ScriptedChatModel(steps=scripts.simple_reply()))

    response = await post_message(async_client, created.id, "你好")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    sandbox = adopt_env.sandboxes[created.id]
    assert sandbox.calls == []  # 零沙箱调用：无 create/adopt/status/kill
    assert sandbox.state.created is False
    assert sandbox.commands == []  # 零命令执行（含 diff 链 git 命令——车道已短路）
    assert adopt_env.manager.phase(created.id) == "none"
    assert adopt_env.manager.open_usage_segment(created.id) is None
    assert adopt_env.manager.usage_segments(created.id) == ()

    async with get_session_factory()() as db:
        row = await db.get(SessionRow, uuid.UUID(created.id))
    assert row is not None
    assert row.sandbox_id is None  # 未 provision 即不写回


async def test_message_run_persists_session_sandbox_id(adopt_env, async_client) -> None:
    """首次工具调用才 provision：`sessions.sandbox_id` 在懒 provision 后落库（时点后移）。"""
    created = await create_session(async_client, title="sandbox-id 落库")
    adopt_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))

    response = await post_message(async_client, created.id, "列一下目录")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    sandbox = adopt_env.sandboxes[created.id]
    expected = sandbox.id
    persisted = await _poll_session_sandbox_id(created.id)

    assert persisted == expected
    assert sandbox.calls == ["create"]  # 懒加载：首条工具调用（ls）走 create 且恰好一次
    assert adopt_env.manager.stable_phase(created.id) == "running"


async def test_ensure_adopts_persisted_id_when_manager_empty(
    adopt_env, async_client, db_session
) -> None:
    """manager 无在册但 DB 有持久 id → 首次工具调用懒 adopt（不 create），run 照常完成。"""
    created = await create_session(async_client, title="adopt 重连")
    persisted = "sbx-from-previous-life"
    # 模拟上一进程生命周期留下的持久 id：直接改会话行并提交。
    # 必须经 db_session（请求同源会话）——create_session 只 flush，事务边界在 get_db；
    # 这里 commit 同时把会话行本身落库（与 post_message 首次 commit 的生效路径一致）。
    row = await db_session.get(SessionRow, uuid.UUID(created.id))
    assert row is not None
    row.sandbox_id = persisted
    await db_session.commit()

    adopt_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    response = await post_message(async_client, created.id, "继续上次的活儿")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    sandbox = adopt_env.sandboxes[created.id]
    assert sandbox.calls == ["adopt"]  # 懒加载：首工具调用走重连（调用轨迹无 create）
    assert sandbox.id == persisted  # 实例被重连到持久 id
    assert await _poll_session_sandbox_id(created.id) == persisted  # 落库值保持一致


async def test_lazy_adopt_failure_falls_back_to_create_and_persists_new_id(
    adopt_env, async_client, db_session
) -> None:
    """P1 自愈：持久 id 已失效 → 首工具调用 adopt 失败 → 同一次使用内降级 create 兜底。

    语义对齐 eager「adopt 失败转重建」的 AC-002-4 告知链：create 成功后以
    reason="recreated" 的 running 生命周期事件告知，新 id 写回 sessions.sandbox_id
    （下次进程重启可 adopt 到活实例而非死 id）；run 正常完成、无悬挂计费段。
    """
    created = await create_session(async_client, title="adopt 失败降级")
    dead_id = "sbx-dead-id"
    # 播种口径同 test_ensure_adopts_persisted_id_when_manager_empty（db_session 显式提交）
    row = await db_session.get(SessionRow, uuid.UUID(created.id))
    assert row is not None
    row.sandbox_id = dead_id
    await db_session.commit()

    adopt_env.flags["fail_adopt"] = True  # 工厂期注入：下一只 FakeSandbox 的 adopt 必失败
    adopt_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    response = await post_message(async_client, created.id, "活儿还在吗")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    sandbox = adopt_env.sandboxes[created.id]
    assert sandbox.calls == ["adopt", "create"]  # adopt 失败 → 同一次使用内 create 兜底
    assert sandbox.id != dead_id
    assert adopt_env.manager.stable_phase(created.id) == "running"
    assert adopt_env.manager.open_usage_segment(created.id) is not None

    run = await latest_run(db_session, created.id)
    assert run.status == "completed"

    # 新 id 写回 sessions.sandbox_id（下次重启 adopt 到的是活实例，不是死 id）
    assert await _poll_session_sandbox_id(created.id) == sandbox.id

    # 告知语义：running 生命周期事件带 reason="recreated"（对齐 eager 重建告知面）
    rows = await events_of(db_session, created.id)
    recreated = [
        payload
        for payload in event_payload(rows, "sandbox_output")
        if payload.get("channel") == "lifecycle"
        and payload.get("phase") == "running"
        and payload.get("reason") == "recreated"
    ]
    assert len(recreated) == 1
    assert recreated[0]["sandbox_id"] == sandbox.id


async def test_second_run_after_adopt_failure_reuses_recovered_sandbox(
    adopt_env, async_client, db_session
) -> None:
    """P1 恢复面：adopt 失败降级 create 后，第二条 run 复用已恢复实例（零新 provision）。"""
    created = await create_session(async_client, title="adopt 降级后续跑")
    dead_id = "sbx-dead-id"
    row = await db_session.get(SessionRow, uuid.UUID(created.id))
    assert row is not None
    row.sandbox_id = dead_id
    await db_session.commit()

    adopt_env.flags["fail_adopt"] = True
    adopt_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    response = await post_message(async_client, created.id, "第一轮")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    first = adopt_env.sandboxes[created.id]
    assert first.calls == ["adopt", "create"]  # 第一轮完成自愈

    adopt_env.set_model(ScriptedChatModel(steps=scripts.tool_loop_read_only()))
    response = await post_message(async_client, created.id, "第二轮")
    assert response.status_code == 200, response.text
    await wait_run_state(async_client, created.id, "idle")

    assert adopt_env.sandboxes[created.id] is first  # 工厂未再造实例（在册复用）
    assert first.calls == ["adopt", "create"]  # 第二条 run 零新增 provision 调用
    assert adopt_env.manager.stable_phase(created.id) == "running"
