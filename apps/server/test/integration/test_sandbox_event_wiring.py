"""integration 层：沙箱事件桥 → EventPipeline → DB/总线 的真实链路（M2 集成装配）。

与 unit 层的分账：unit 只验载荷构造器与注解链的纯面；本文件用**真库 + 真总线 +
FakeSandbox** 验证——

1. manager 相位迁移（create）→ `sandbox_output(lifecycle)` 落库（events 表）+ 广播
   （订阅队列可见）；
2. 带工具上下文的命令执行 → `sandbox_output(stdout)` 落库 + 广播（exec_id 注解贯通）；
3. 封口段 → `sandbox_usage` 行落库（AC-015-4 的持久化半边）；
4. 无工具上下文的输出块（契约 stdout/stderr 必带 exec_id）不产生事件、账本不受影响。

替身边界（A3 §9.2）：沙箱 = FakeSandbox（真 tmp + 真子进程）；事件出口 = 真
EventPipeline（TypeAdapter 唯一闸门真实生效）；模型不参与（不起图）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.db import dispose_engine, get_session_factory
from app.dependencies import build_sandbox_event_bridges
from app.events import EventPipeline
from app.models import Event as EventRow
from app.models import SandboxUsage
from app.models import Session as SessionRow
from app.sandbox.manager import SandboxManager
from app.utils.exec_context import EXEC_ID_CONTEXT
from test.support.fake_sandbox import ExecStep, FakeSandbox

pytestmark = pytest.mark.requires_db

SESSION_TITLE = "wiring-int"


async def _next_event(subscription: Any, timeout: float = 5.0) -> Any:
    """取下一条广播事件（桥接发射是后台任务，逐条等而非轮询）；Any 面 = 契约联合的动态断言。"""
    return await asyncio.wait_for(subscription.get(), timeout)


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[SandboxManager, EventPipeline, str]]:
    """真管线 + 真库会话行 + 桥接后的 manager；返回 (manager, pipeline, session_id)。

    每测 TRUNCATE（conftest）保证 sessions 表只有本测的行；会话 id 由 RETURNING 回填。
    桥的 lookup 经两阶段 cell 绑定到**本测的** manager（生产单例路径绑定全局 manager，
    见 dependencies._default_sandbox_lookup——manager 持桥、桥查 manager 的环由闭包
    延迟求值解开，测试侧同理）。
    """
    pipeline = EventPipeline()
    cell: list[SandboxManager] = []
    on_lifecycle, on_output, on_usage = build_sandbox_event_bridges(
        pipeline, lookup=lambda session_id: cell[0].get(session_id)
    )
    manager = SandboxManager(
        factory=lambda provider, session_id: FakeSandbox(
            root=tmp_path / session_id[:8],
            session_id=session_id,
            sandbox_id=f"sbx-{session_id[:8]}",
            exec_script=[ExecStep(stdout_chunks=("hello from sandbox\n",), exit_code=0)],
        ),
        on_lifecycle=on_lifecycle,
        on_output=on_output,
        on_usage_segment=on_usage,
    )
    cell.append(manager)

    factory = get_session_factory()
    async with factory() as db:
        row = SessionRow(
            title=SESSION_TITLE,
            model="qwen3.7-plus",
            reasoning_effort="high",
            sandbox_provider="bailian",
        )
        db.add(row)
        await db.flush()  # server_default gen_random_uuid() 经 RETURNING 回填
        session_id = str(row.id)
        await db.commit()

    yield manager, pipeline, session_id

    manager.shutdown()
    # 收尾沉降：桥接的后台任务（shutdown pause 触发的 usage 封口段落库）要落完才能关
    # 事件循环——每测一个事件循环 × 共享引擎单例的测试基建竞态，同 run_service 停止
    # 测试的 0.3s 沉降口径（asyncpg 协议层取消清理由异步完成）。
    await asyncio.sleep(0.3)
    # 释放连接池：pytest-asyncio 每测一个事件循环，池里上一循环创建的 asyncpg 连接在
    # 下一循环不可用（conftest.db_session 同款收尾口径；本 fixture 自取短会话，不走它）。
    await dispose_engine()


async def test_lifecycle_and_output_reach_bus_and_db(
    wired: tuple[SandboxManager, EventPipeline, str],
) -> None:
    """create 相位迁移 + 带工具上下文的命令输出 → 事件落库且广播可见；封口段落 sandbox_usage。"""
    manager, pipeline, session_id = wired
    subscription = pipeline.bus.subscribe(session_id)
    try:
        # ① create：creating（无 id，诚实跳过）→ running（有 id，广播）
        manager.create(session_id, provider="bailian")
        lifecycle = await _next_event(subscription)
        assert lifecycle.type == "sandbox_output"
        assert lifecycle.channel == "lifecycle"
        assert lifecycle.phase == "running"
        assert lifecycle.sandbox_id == f"sbx-{session_id[:8]}"

        # ② 带工具上下文的命令执行：stdout 块经 EXEC_ID_CONTEXT 注解 → 域事件
        token = EXEC_ID_CONTEXT.set("call-wiring-1")
        try:
            manager.run_command(session_id, "echo hello from sandbox")
        finally:
            EXEC_ID_CONTEXT.reset(token)
        output = await _next_event(subscription)
        assert output.type == "sandbox_output"
        assert output.channel == "stdout"
        assert "hello from sandbox" in output.data
        assert output.exec_id == "call-wiring-1"

        # ③ release → killed 生命周期事件 + 封口段 usage 落库
        manager.release(session_id, reason="test_done")
        killed = await _next_event(subscription)
        assert killed.phase == "killed"
    finally:
        pipeline.bus.unsubscribe(subscription)

    # ④ events 表落库（先落库后广播的持久化半边：总线可见 ⟹ 落库已 COMMIT）
    factory = get_session_factory()
    async with factory() as db:
        total = await db.scalar(
            select(func.count())
            .select_from(EventRow.__table__)
            .where(EventRow.session_id == uuid.UUID(session_id))
        )
        assert total == 3  # running + stdout + killed，恰好三行

        usage_rows = list(
            await db.scalars(
                select(SandboxUsage).where(SandboxUsage.session_id == uuid.UUID(session_id))
            )
        )
        assert len(usage_rows) == 1  # create→release 之间的一个 running 段已封口
        usage = usage_rows[0]
        assert usage.phase == "running"
        assert usage.provider == "bailian"
        assert usage.sandbox_id == f"sbx-{session_id[:8]}"
        assert usage.ended_at is not None
        assert usage.duration_seconds is not None and usage.duration_seconds >= 0
        assert usage.estimated_cost is not None
        assert type(usage.estimated_cost) is Decimal  # F8：numeric(12,6) 读回即 Decimal


async def test_output_without_tool_context_is_not_emitted(
    wired: tuple[SandboxManager, EventPipeline, str],
) -> None:
    """无工具上下文（EXEC_ID_CONTEXT 空）的输出块：契约无 exec_id 不可发 → 事件面安静。"""
    manager, pipeline, session_id = wired
    subscription = pipeline.bus.subscribe(session_id)
    try:
        manager.create(session_id, provider="bailian")
        lifecycle = await _next_event(subscription)
        assert lifecycle.phase == "running"

        manager.run_command(session_id, "echo hello from sandbox")  # 无上下文：stdout 块被跳过
        with pytest.raises(asyncio.TimeoutError):
            await _next_event(subscription, timeout=0.3)
    finally:
        pipeline.bus.unsubscribe(subscription)
