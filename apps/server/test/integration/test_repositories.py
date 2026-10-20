"""integration 层：十表 CRUD 与游标分页（test-plan-m2 §2.4 的库侧证据 / §5 T6）。

- **十表 CRUD**：逐表 增（服务端默认值生效）→ 查（按 id 回读）→ 改（标量列）→ 删（回读 0 行）；
  规格表刻意与 `Base.metadata.tables` 对齐——新增表会让本文件红，强制补覆盖；
- **约束真实生效**：外键（未知 session_id）/ CHECK（枚举值域外）/ 唯一
  （(session_id, seq) 与 (session_id, round)）三类都在 DB 侧断言，证明迁移产物不是
  「建了表但没建约束」；
- **游标分页**：`app/schemas/internal.py` 的 Cursor 编解码 + (created_at, id) 键集分页走通
  全量（消息车道 GET /sessions/{id}/messages 复用同一模式）；非法游标 → InvalidArgument
  （ERROR_MAP → 契约 INVALID_ARGUMENT 422）。

全文件 requires_db：conftest 重建 DB_NAME 指向的测试库并 `alembic upgrade head`；本车道以
`DB_NAME=lcode_session_test` 运行（并发隔离约定）。游标编解码是纯函数，按 test-plan 的
落点与分页用例同居本文件（缺库时随本文件一并显式 skip，reason 可见）。
"""

import base64
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError

from app.exception.common import InvalidArgument
from app.models import (
    Approval,
    Base,
    DiffReview,
    Event,
    GitBaseline,
    Message,
    ModelCall,
    Run,
    SandboxUsage,
    Session,
    ToolCall,
)
from app.schemas.internal import Cursor, decode_cursor, encode_cursor

pytestmark = pytest.mark.requires_db

MOMENT = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


async def _anchor(db_session) -> tuple[uuid.UUID, uuid.UUID]:
    """锚点两行（会话 + 运行）：其余八表的外键依赖，返回 (session_id, run_id)。"""
    session = Session(model="qwen3.7-plus", reasoning_effort="high", sandbox_provider="bailian")
    db_session.add(session)
    await db_session.flush()

    run = Run(session_id=session.id, status="running")
    db_session.add(run)
    await db_session.flush()

    return session.id, run.id


# ---------------------------------------------------------------- 十表 CRUD


@dataclass(frozen=True)
class _CrudSpec:
    """一行规格：表名 + ORM 类 + 构造器（session_id/run_id → 列 kwargs）+ 可变更标量列。"""

    table: str
    model: Any  # SQLAlchemy ORM 类：十表各异，这里只做统一驱动
    build: Callable[[uuid.UUID, uuid.UUID], dict[str, Any]]
    update_column: str
    update_value: Any


_CRUD_SPECS: list[_CrudSpec] = [
    _CrudSpec(
        "sessions",
        Session,
        lambda s, r: {
            "model": "qwen3.7-plus",
            "reasoning_effort": "high",
            "sandbox_provider": "bailian",
        },
        "title",
        "改过的标题",
    ),
    _CrudSpec(
        "runs",
        Run,
        lambda s, r: {"session_id": s, "status": "running"},
        "status",
        "completed",
    ),
    _CrudSpec(
        "messages",
        Message,
        lambda s, r: {"session_id": s, "run_id": r, "role": "user", "content": "你好"},
        "content",
        "改过的内容",
    ),
    _CrudSpec(
        "tool_calls",
        ToolCall,
        lambda s, r: {
            "session_id": s,
            "run_id": r,
            "tool_call_id": "call-1",
            "tool": "execute",
            "args": {"command": "ls -la"},
            "status": "success",
        },
        "status",
        "error",
    ),
    _CrudSpec(
        "approvals",
        Approval,
        lambda s, r: {
            "session_id": s,
            "run_id": r,
            "tool_call_id": "call-2",
            "tool": "write_file",
            "args": {},
            "risk_level": "high",
        },
        "status",
        "approved",
    ),
    _CrudSpec(
        "diff_reviews",
        DiffReview,
        lambda s, r: {
            "session_id": s,
            "run_id": r,
            "round": 1,
            "baseline_kind": "init",
            "baseline_ref": "lcode-baseline/round-0",
            "baseline_commit": "0" * 40,
        },
        "verdict",
        "accepted",
    ),
    _CrudSpec(
        "git_baselines",
        GitBaseline,
        lambda s, r: {
            "session_id": s,
            "round": 0,
            "kind": "head",
            "ref": "HEAD",
            "commit": "0" * 40,
        },
        "kind",
        "accepted",
    ),
    _CrudSpec(
        "model_calls",
        ModelCall,
        lambda s, r: {
            "session_id": s,
            "run_id": r,
            "model": "qwen3.7-plus",
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "status": "success",
        },
        "status",
        "error",
    ),
    _CrudSpec(
        "sandbox_usage",
        SandboxUsage,
        lambda s, r: {
            "session_id": s,
            "provider": "bailian",
            "sandbox_id": "sbx-1",
            "phase": "running",
            "started_at": MOMENT,
        },
        "phase",
        "paused",
    ),
    _CrudSpec(
        "events",
        Event,
        lambda s, r: {"session_id": s, "seq": 0, "type": "message_delta", "data": {"kind": "text"}},
        "type",
        "message_finished",
    ),
]


async def test_crud_all_ten_tables(db_session) -> None:
    """AC-004-1 库侧证据：十表 增/查/改/删 四步都通，user_id 与 created_at 服务端默认生效。"""
    assert {spec.table for spec in _CRUD_SPECS} == set(Base.metadata.tables), (
        "规格表必须覆盖全部业务表（新增表时补齐，否则本用例失去守门意义）"
    )

    session_id, run_id = await _anchor(db_session)

    for spec in _CRUD_SPECS:
        row = spec.model(**spec.build(session_id, run_id))
        db_session.add(row)
        await db_session.flush()  # 约束校验点：FK/CHECK/唯一都在这一行真实生效

        # 增：服务端默认值（uuid / user_id / created_at）已回填
        assert row.id is not None  # gen_random_uuid()
        assert row.user_id == "local"  # NFR-008：P0 固定单用户
        assert row.created_at is not None  # now()

        # 查：按 id 回读恰好一行
        found = await db_session.scalar(select(spec.model).where(spec.model.id == row.id))
        assert found is not None, f"{spec.table} 写入后回读不到"

        # 改：标量列写回并回读确认
        setattr(found, spec.update_column, spec.update_value)
        await db_session.flush()
        updated = await db_session.scalar(
            select(getattr(spec.model, spec.update_column)).where(spec.model.id == row.id)
        )
        assert updated == spec.update_value

        # 删：删除后回读 0 行
        await db_session.delete(found)
        await db_session.flush()
        remaining = await db_session.scalar(
            select(func.count()).select_from(spec.model).where(spec.model.id == row.id)
        )
        assert remaining == 0, f"{spec.table} 删除后仍可查到"


# ---------------------------------------------------------------- 约束（DB 侧真实生效）


async def test_foreign_key_rejects_unknown_session(db_session) -> None:
    """外键真实生效：未知 session_id 的消息被数据库拒绝（不是裸列）。"""
    db_session.add(Message(session_id=uuid.uuid4(), role="user", content="孤儿消息"))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_check_constraint_rejects_illegal_enum(db_session) -> None:
    """CHECK 值域真实生效（varchar + CHECK 口径，A3 §7.1）。"""
    db_session.add(
        Session(model="qwen3.7-plus", reasoning_effort="ultra", sandbox_provider="bailian")
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_unique_index_on_events_session_seq(db_session) -> None:
    """(session_id, seq) 唯一：seq 分配的有界重试依赖这条唯一索引兜底（A3 §6.3）。"""
    session_id, _run_id = await _anchor(db_session)
    db_session.add_all(
        [
            Event(session_id=session_id, seq=0, type="message_delta", data={}),
            Event(session_id=session_id, seq=0, type="message_finished", data={}),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize(
    ("orm_model", "build"),
    [
        (
            DiffReview,
            lambda s: {
                "session_id": s,
                "round": 1,
                "baseline_kind": "init",
                "baseline_ref": "lcode-baseline/round-0",
                "baseline_commit": "0" * 40,
            },
        ),
        (
            GitBaseline,
            lambda s: {
                "session_id": s,
                "round": 0,
                "kind": "head",
                "ref": "HEAD",
                "commit": "0" * 40,
            },
        ),
    ],
    ids=["diff_reviews", "git_baselines"],
)
async def test_unique_index_on_session_round(db_session, orm_model, build) -> None:
    """(session_id, round) 唯一：diff 审查轮次与基线链各自唯一（契约 IndexSpec）。"""
    session_id, _run_id = await _anchor(db_session)
    db_session.add_all([orm_model(**build(session_id)), orm_model(**build(session_id))])

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------- 游标编解码


async def test_cursor_codec_roundtrip_and_rejects_garbage() -> None:
    """Cursor 编解码：base64 JSON {created_at,id} 往返一致；非法输入 → InvalidArgument。"""
    identity = str(uuid.uuid4())
    token = encode_cursor(Cursor(created_at=MOMENT, id=identity))

    assert decode_cursor(token) == Cursor(created_at=MOMENT, id=identity)

    def _token(payload: object) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    bad_tokens = [
        "",  # 空串
        "not-base64!!",  # 非 base64 字母表
        _token("不是对象"),  # JSON 合法但非对象
        _token({}),  # 缺字段
        _token({"created_at": MOMENT.isoformat()}),  # 缺 id
        _token({"created_at": "不是时间", "id": identity}),  # 时间不可解析
        _token({"created_at": MOMENT.isoformat(), "id": "not-a-uuid"}),  # id 非 UUID
    ]
    for bad in bad_tokens:
        with pytest.raises(InvalidArgument):
            decode_cursor(bad)


# ---------------------------------------------------------------- 键集分页


async def test_cursor_pagination_walks_messages_without_overlap(db_session) -> None:
    """(created_at, id) 键集分页逐页走通全量：不重不漏（消息车道同款查询模式）。"""
    session_id, _run_id = await _anchor(db_session)
    for index in range(5):
        db_session.add(
            Message(
                session_id=session_id,
                role="user",
                content=f"第 {index} 条",
                created_at=MOMENT + timedelta(minutes=index),
            )
        )
    await db_session.commit()

    seen = await _walk_pages(db_session, session_id, page_size=2)

    assert seen == [f"第 {index} 条" for index in reversed(range(5))]  # 倒序：最新在最前
    assert len(seen) == len(set(seen))  # 无重复


async def test_cursor_pagination_survives_identical_timestamps(db_session) -> None:
    """同一 created_at 的多行靠 id 次序不重不漏——id 是游标的第二排序键。"""
    session_id, _run_id = await _anchor(db_session)
    for index in range(3):
        db_session.add(
            Message(
                session_id=session_id, role="user", content=f"同时刻 {index}", created_at=MOMENT
            )
        )
    await db_session.commit()

    seen = await _walk_pages(db_session, session_id, page_size=2)

    assert sorted(seen) == ["同时刻 0", "同时刻 1", "同时刻 2"]
    assert len(seen) == len(set(seen))


async def _walk_pages(db_session, session_id: uuid.UUID, *, page_size: int) -> list[str]:
    """按 (created_at, id) 倒序分页逐页取，返回全部 content（键集分页的标准走法）。

    消息车道的 GET /sessions/{id}/messages 复用同一模式：请求值 = 上一页响应的
    `next_cursor`，服务端用 `decode_cursor` 还原水位后做行值比较（无需 offset）。
    """
    seen: list[str] = []
    cursor: str | None = None
    while True:
        statement = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(page_size)
        )
        if cursor is not None:
            position = decode_cursor(cursor)
            statement = statement.where(
                tuple_(Message.created_at, Message.id)
                < (position.created_at, uuid.UUID(position.id))
            )
        rows = list(await db_session.scalars(statement))
        seen.extend(str(row.content) for row in rows)

        if len(rows) < page_size:  # 尾页：契约 next_cursor 为空
            return seen
        cursor = encode_cursor(Cursor(created_at=rows[-1].created_at, id=str(rows[-1].id)))
