"""unit 层：diff 编排的纯逻辑面（M3 Phase 4；零库零网络）。

覆盖不依赖数据库/git 的子面：

- `build_rework_message`：驳回返工的措辞契约（必须让模型理解为「上一轮被驳回 + 原因」；
  无 feedback 时也有明确说明）；
- `_safe_emit_resolved`：事件出口故障隔离——`diff_resolved` 发不出去不得让已提交的裁决
  变成 500（裁决事实以库为准，客户端可经详情重读）；
- `_to_detail`：ORM 行（files 为内部超集）→ 契约 `DiffReviewDetail` 的投影（摘要不含
  patch/blob_ref；baseline 三元组原样透出）。

真链路（基线链/轮次/supersede/裁决/返工）在 `test/integration/test_diff_review_flow.py`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError

from app.exception.diff import DiffAlreadyResolved
from app.models import DiffReview
from app.sandbox.git import PATCH_LIMIT_BYTES, WorkspaceFileDiff
from app.services import diff_service
from app.services.diff_service import (
    DiffService,
    _file_payload,
    _to_detail,
    _with_system_note,
    build_rework_message,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class _BoomPipeline:
    """事件出口替身：任何 emit 都炸（验证故障隔离）。"""

    async def emit(self, payload: Any) -> Any:  # noqa: ARG002
        raise RuntimeError("event bus down")


def _row(*, verdict: str = "pending") -> DiffReview:
    session_id = uuid.uuid4()
    return DiffReview(
        id=uuid.uuid4(),
        user_id="local",
        session_id=session_id,
        run_id=None,
        round=2,
        verdict=verdict,
        baseline_kind="accepted",
        baseline_ref="lcode-baseline/round-1",
        baseline_commit="a" * 40,
        files=[
            {
                "path": "src/deep.py",
                "old_path": None,
                "change": "added",
                "additions": 3,
                "deletions": 0,
                "binary": False,
                "patch": "diff --git a/src/deep.py b/src/deep.py\n+print('hi')\n",
            },
            {
                "path": "bin.dat",
                "old_path": None,
                "change": "added",
                "additions": 0,
                "deletions": 0,
                "binary": True,
                "patch": None,
                "blob_ref": "sandbox:/ws/bin.dat",
            },
            {
                "path": "big.txt",
                "old_path": None,
                "change": "added",
                "additions": 12000,
                "deletions": 0,
                "binary": False,
                "patch": None,
                "truncated": True,
                "blob_ref": "sandbox:/ws/big.txt",
            },
        ],
        additions_total=12003,
        deletions_total=0,
        feedback=None,
        decided_by=None,
        resolved_at=None,
        created_at=NOW,
    )


def test_build_rework_message_carries_round_and_feedback() -> None:
    """驳回返工措辞：明确「上一轮改动被驳回」+ 原文反馈（AC-007-3 的返工语义）。"""
    message = build_rework_message(round_=2, feedback="  把实现改成两行  ")

    assert "第 2 轮" in message
    assert "上一轮改动未通过 diff 审查" in message
    assert "把实现改成两行" in message
    assert "请根据以上意见修改工作区" in message


def test_build_rework_message_without_feedback_still_explains() -> None:
    """无 feedback 的兜底措辞（防御性分支：正常路径不触发自动返工）。"""
    message = build_rework_message(round_=1, feedback="   ")

    assert "第 1 轮" in message
    assert "未给出具体意见" in message


async def test_safe_emit_resolved_swallows_event_failure() -> None:
    """事件出口故障：`_safe_emit_resolved` 只记 ERROR，不把异常抛给裁决路径。"""
    service = DiffService(cast(Any, None), pipeline=cast(Any, _BoomPipeline()))

    await service._safe_emit_resolved(  # noqa: SLF001 —— 锁定的内部不变量（故障隔离）
        _row(verdict="accepted"), feedback=None, decided_by="user", now=NOW
    )


class _RefreshOnlySession:
    """只实现 `refresh` 的会话替身（`_recheck_pending` 的唯一依赖面）。"""

    def __init__(self) -> None:
        self.refreshed: list[Any] = []

    async def refresh(self, row: Any) -> None:
        self.refreshed.append(row)


async def test_recheck_pending_raises_when_verdict_changed() -> None:
    """锁内复核（防未走锁的写者）：git 往返后发现已裁决/被取代 → 409，不做任何 git 写。"""
    session = _RefreshOnlySession()
    service = DiffService(cast(Any, session), pipeline=cast(Any, _BoomPipeline()))

    await service._recheck_pending(_row(verdict="pending"), "rid")  # noqa: SLF001
    assert len(session.refreshed) == 1

    with pytest.raises(DiffAlreadyResolved):
        await service._recheck_pending(_row(verdict="rejected"), "rid")  # noqa: SLF001


def test_to_detail_projects_internal_superset_to_contract() -> None:
    """files 内部超集 → 契约摘要（不含 patch/blob_ref/truncated），totals 原样透出。"""
    detail = _to_detail(_row())

    assert detail.round == 2
    assert detail.verdict == "pending"
    assert (detail.baseline.kind, detail.baseline.ref, detail.baseline.commit) == (
        "accepted",
        "lcode-baseline/round-1",
        "a" * 40,
    )
    assert [item.path for item in detail.files] == ["src/deep.py", "bin.dat", "big.txt"]
    assert detail.files[1].binary is True
    assert (detail.additions_total, detail.deletions_total) == (12003, 0)
    assert detail.additions_total == sum(item.additions for item in detail.files)
    # 契约摘要没有 patch/blob_ref 字段（payload 里的正文引用不外泄到列表投影）
    dumped = detail.files[0].model_dump()
    assert set(dumped) == {"path", "old_path", "change", "additions", "deletions", "binary"}


# ---------------------------------------------------------------- 反馈注记拼接


def test_with_system_note_appends_with_prefix_and_blank_line() -> None:
    """系统注记追加在用户意见尾部：`[系统]` 前缀 + 空行分隔（前端按纯文本展示即可区分）。"""
    merged = _with_system_note("再检查一下", "返工未自动启动：会话已有运行中的任务。")

    assert merged == "再检查一下\n\n[系统] 返工未自动启动：会话已有运行中的任务。"
    # 用户意见在前、系统注记在后（用户原文不被改写）
    assert merged is not None and merged.startswith("再检查一下")


def test_with_system_note_without_note_is_passthrough() -> None:
    """无注记：feedback 原样返回（包括 None —— 事件里保持 None 而不是空串）。"""
    assert _with_system_note("原意见", None) == "原意见"
    assert _with_system_note(None, None) is None


def test_with_system_note_works_without_user_feedback() -> None:
    """没有用户意见时只落注记本身（不允许出现孤零零的空行）。"""
    assert _with_system_note("", "冲突") == "[系统] 冲突"
    assert _with_system_note(None, "冲突") == "[系统] 冲突"


# ---------------------------------------------------------------- files 条目投影


def _entry(**overrides: Any) -> WorkspaceFileDiff:
    base: dict[str, Any] = {
        "path": "src/deep.py",
        "old_path": None,
        "change": "added",
        "additions": 1,
        "deletions": 0,
        "binary": False,
        "patch": "diff --git a/src/deep.py b/src/deep.py\n+print('hi')\n",
    }
    base.update(overrides)
    return WorkspaceFileDiff(**base)


def test_file_payload_marks_skipped_patch_distinctly() -> None:
    """未拉取（oversize/budget）：patch=null + truncated + patch_skipped + 预估量级。"""
    oversize = _file_payload(
        _entry(patch=None, patch_skipped="oversize", estimated_patch_bytes=2_000_000),
        root="/ws",
        baseline_commit="a" * 40,
    )
    budget = _file_payload(
        _entry(patch=None, patch_skipped="budget", estimated_patch_bytes=64),
        root="/ws",
        baseline_commit="a" * 40,
    )

    assert oversize["patch"] is None
    assert oversize["truncated"] is True
    assert oversize["patch_skipped"] == "oversize"
    assert oversize["estimated_patch_bytes"] == 2_000_000
    assert oversize["blob_ref"] == "sandbox:/ws/src/deep.py"
    assert budget["patch_skipped"] == "budget"
    assert "patch_bytes" not in oversize  # 没读过，不能报「实际字节」


def test_file_payload_truncates_patch_over_limit() -> None:
    """最后一道：实际 patch 超 1MB（numstat 预估失效）→ 读后截断，标实际字节。"""
    payload = _file_payload(
        _entry(patch="x" * (PATCH_LIMIT_BYTES + 10)),
        root="/ws",
        baseline_commit="a" * 40,
    )

    assert payload["patch"] is None
    assert payload["truncated"] is True
    assert payload["patch_bytes"] == PATCH_LIMIT_BYTES + 10
    assert "patch_skipped" not in payload


def test_file_payload_keeps_small_patch_and_binary_blob_ref() -> None:
    """小 patch 原样落库；binary 不带 truncated（用 binary 标记区分）。"""
    small = _file_payload(_entry(), root="/ws", baseline_commit="a" * 40)
    binary = _file_payload(
        _entry(path="bin.dat", binary=True, patch=None),
        root="/ws",
        baseline_commit="a" * 40,
    )
    deleted_binary = _file_payload(
        _entry(path="gone.bin", change="deleted", binary=True, patch=None),
        root="/ws",
        baseline_commit="b" * 40,
    )

    assert small["patch"] == _entry().patch
    assert "truncated" not in small
    assert binary["binary"] is True and "truncated" not in binary
    assert binary["blob_ref"] == "sandbox:/ws/bin.dat"
    assert deleted_binary["blob_ref"] == f"git:{'b' * 40}:gone.bin"


# ---------------------------------------------------------------- 基线并发双写兜底


class _CommitConflictSession:
    """短事务会话替身：commit 恒撞唯一约束（模拟并发双写）。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.rolled_back = False

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        raise IntegrityError("INSERT INTO git_baselines", {}, Exception("duplicate key"))

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _CommitConflictSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _Factory:
    """`async_sessionmaker` 的替身：每次 `factory()` 只给一个新会话。"""

    def __init__(self) -> None:
        self.sessions: list[_CommitConflictSession] = []

    def __call__(self) -> _CommitConflictSession:
        session = _CommitConflictSession()
        self.sessions.append(session)
        return session


async def test_ensure_baseline_conflict_reuses_committed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发双写撞唯一约束：回滚后复用已提交行（不把 IntegrityError 抛给收口）。

    该分支是 `uq_git_baselines_session_round` 的兜底：两次收口同时补 round-0 时，
    后到者必须复用先到者的行，否则 run 收口的 diff 钩子会失败（虽被隔离，但会丢审查）。
    """
    existing = SimpleNamespace(round=0, kind="head", ref="HEAD", commit="a" * 40)
    calls = {"n": 0}

    async def fake_latest(session: Any, session_id: Any) -> Any:  # noqa: ARG001
        calls["n"] += 1
        # 调用序：① 首次检查无行 → ② 插入前重查仍无行 → ③ 冲突回滚后重查命中已提交行
        return existing if calls["n"] >= 3 else None

    monkeypatch.setattr(diff_service, "latest_baseline", fake_latest)

    factory = _Factory()
    service = DiffService(
        cast(Any, None),
        pipeline=cast(Any, _BoomPipeline()),
        session_factory=cast(Any, factory),
    )
    cast(Any, service)._workspace = lambda client: SimpleNamespace(  # noqa: ARG005
        layout=SimpleNamespace(root="/ws", is_repo=True, head="a" * 40)
    )
    cast(Any, service)._live_client = lambda session_id: object()  # noqa: ARG005

    snapshot = await service.ensure_baseline(str(uuid.uuid4()))

    assert (snapshot.kind, snapshot.ref, snapshot.commit) == ("head", "HEAD", "a" * 40)
    assert len(factory.sessions) == 2
    assert calls["n"] == 3
    assert factory.sessions[1].added  # 插入尝试确实发生（随后撞唯一约束）
    assert factory.sessions[1].rolled_back is True
