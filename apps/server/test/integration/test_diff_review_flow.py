"""integration：diff 审查链（M3 Phase 4，真 PG；REQ-007 / AC-007-1~5）。

覆盖（对应 m3-plan D6/D7 与 Phase 4 的验收面）：

1. **基线链**：git 输入记 `kind=head`（不做 git 写）→ 第一轮 round 1；accepted 推进
   基线（commit + tag `lcode-baseline/round-N`）→ 下一轮对照新基线、轮次 +1；rejected
   **不推进**基线（下一轮仍对照同一基线）；
2. **无变化不发**：工作区与基线一致 → 不建行、不发 `diff_ready`（D6）；
3. **supersede**：pending 存在时新轮把旧轮置 `rejected`/`decided_by=system`/
   `feedback=已被第 N 轮审查取代` 并转 `diff_resolved`；
4. **diff_ready 契约**：事件载荷过契约校验（`row_to_event`）+ totals 与 files 求和一致
   （诚实性强校验）；`files` 覆盖新增（含中文/空格/空文件/二进制）/修改/删除/rename；
5. **三端点**：详情 / 逐文件 patch（`{file_path:path}`，含子目录与空格）/ 裁决；
   404 `DIFF_REVIEW_NOT_FOUND`、404 `NOT_FOUND`（文件不在本轮）、409
   `DIFF_ALREADY_RESOLVED`；
6. **体积保护**：binary → `patch=null` + `blob_ref`；>1MB 文本 patch → 截断（REST
   `patch=null` + `blob_ref`，jsonb 标 `truncated`）；
7. **驳回自动返工**：rejected + feedback → 新 run 且首条用户消息含反馈；会话已有活动 run
   → 只记录裁决与系统提示、不建 run；
8. **失败隔离**：diff 计算抛错 → run 仍 `completed`、无 `diff_ready`。

替身边界：沙箱=FakeSandbox（真 tmp + 真子进程 → 真 git），事件管线/DB 走真件；
git 验证走**独立 git 进程**（`git -C <tmp>`，不经被测代码）。
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.dependencies import (
    get_checkpointer,
    get_event_pipeline,
    get_model_factory,
    get_sandbox_manager,
)
from app.exception.common import InvalidArgument
from app.main import app
from app.models import DiffReview, GitBaseline
from app.models import Event as EventRow
from app.models import Message as MessageRow
from app.models import Run as RunRow
from app.sandbox.base import SandboxError
from app.sandbox.git import GitWorkspace, GitWorkspaceError
from app.sandbox.manager import SandboxManager
from app.services import diff_service as diff_service_module
from app.services.diff_service import DiffService, prepare_review_short_tx
from app.services.message_service import MessageService
from lcode_contracts.events import DiffReadyEvent, DiffResolvedEvent
from lcode_contracts.rest import DiffFileDetail, DiffReviewDetail, DiffVerdictRequest
from test.support import scripts
from test.support.approval_env import (
    create_session,
    event_payload,
    events_of,
    latest_run,
    post_message,
    unwrap,
    wait_run_state,
)
from test.support.fake_llm import ScriptedChatModel
from test.support.fake_sandbox import FakeSandbox

pytestmark = pytest.mark.requires_db

SESSIONS = "/api/v1/sessions"
REVIEWS = "/api/v1/diff-reviews"


# --------------------------------------------------------------------------- 环境


class DiffEnv:
    """运行时替身：FakeSandbox（每个会话一个 tmp 工作区）+ 记录工作区路径。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.roots: dict[str, Path] = {}
        self.sandboxes: dict[str, FakeSandbox] = {}

        def _factory(provider: str, session_id: str) -> FakeSandbox:  # noqa: ARG001
            root = tmp_path / f"ws-{session_id}"
            root.mkdir(parents=True, exist_ok=True)
            self.roots[session_id] = root
            sandbox = FakeSandbox(root=root, session_id=session_id)
            self.sandboxes[session_id] = sandbox
            return sandbox

        self.manager = SandboxManager(factory=_factory)
        self.model = ScriptedChatModel(steps=[])

    def set_model(self, model: ScriptedChatModel) -> None:
        self.model = model

    def workspace(self, session_id: str) -> Path:
        return self.roots[session_id]

    def model_factory(self, role: str) -> ScriptedChatModel:
        self.model.role = role
        return self.model


@pytest.fixture
def diff_env(async_client, tmp_path) -> Iterator[DiffEnv]:  # noqa: ARG001 —— 依赖它确保 app/DB 就绪
    env = DiffEnv(tmp_path)
    saver = InMemorySaver()
    app.dependency_overrides[get_sandbox_manager] = lambda: env.manager
    app.dependency_overrides[get_checkpointer] = lambda: saver
    app.dependency_overrides[get_model_factory] = lambda: env.model_factory
    yield env


# --------------------------------------------------------------------------- git 助手


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=check
    )


def _git(root: Path, *args: str) -> str:
    return _run_git(root, *args).stdout.strip()


def _commit_all(root: Path, message: str = "base") -> str:
    _run_git(root, "add", "-A")
    _run_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _seed_repo(root: Path) -> str:
    """建一个 git 仓库：两处基础文件 + 一棵目录（供 rename/删除/子目录 patch 用）。"""
    root.mkdir(parents=True, exist_ok=True)
    _run_git(root, "init", "-q")
    (root / "doc.md").write_text("line1\nline2\n", encoding="utf-8")
    (root / "remove.md").write_text("bye\n", encoding="utf-8")
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "gone.bin").write_bytes(b"\x00gone")
    nested = root / "dir with space"
    nested.mkdir()
    (nested / "old.txt").write_text("moved\n", encoding="utf-8")
    return _commit_all(root)


def _mutate_workspace(root: Path) -> None:
    """制造六类改动：新增（中文/空格/空/二进制）、修改、删除、rename。"""
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "deep.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "中文.txt").write_text("世界\n", encoding="utf-8")
    (root / "space name.md").write_text("a\n", encoding="utf-8")
    (root / "empty_new.txt").write_text("", encoding="utf-8")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (root / "doc.md").write_text("line1\nline2 changed\n", encoding="utf-8")
    (root / "remove.md").unlink()
    (root / "gone.bin").unlink()
    _run_git(root, "mv", "dir with space/old.txt", "dir with space/new.txt")


async def _prepare(env: DiffEnv, session_id: str):
    return await prepare_review_short_tx(
        session_id=session_id,
        run_id=None,
        user_id="local",
        manager=env.manager,
        pipeline=get_event_pipeline(),
    )


async def _events_of_type(db, session_id: str, event_type: str) -> list[EventRow]:
    rows = await events_of(db, session_id)
    return [row for row in rows if row.type == event_type]


async def _reviews_of(db, session_id: str) -> list[DiffReview]:
    return list(
        await db.scalars(
            select(DiffReview)
            .where(DiffReview.session_id == uuid.UUID(session_id))
            .order_by(DiffReview.round)
        )
    )


async def _baselines_of(db, session_id: str) -> list[GitBaseline]:
    return list(
        await db.scalars(
            select(GitBaseline)
            .where(GitBaseline.session_id == uuid.UUID(session_id))
            .order_by(GitBaseline.round)
        )
    )


async def _create_session_with_sandbox(env: DiffEnv, client, db) -> str:
    detail = await create_session(client, title="diff 审查")
    # async_client 的 get_db override 直接复用 db_session（请求结束时不在 app 侧 commit）：
    # 短事务会话（事件管线/基线落行）要看得到会话行，必须先提交。
    await db.commit()
    env.manager.create(detail.id)
    return detail.id


# --------------------------------------------------------------------------- 1. 第一轮 from HEAD


async def test_prepare_review_round_one_against_head(diff_env, async_client, db_session) -> None:
    """git 输入：基线 kind=head（不做 git 写）→ round 1，文件分类/totals/事件全对位。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    head = _seed_repo(root)
    _mutate_workspace(root)

    review = await _prepare(diff_env, session_id)
    assert review is not None, "有改动必须产出审查"
    assert review.round == 1
    assert review.verdict == "pending"
    assert review.baseline.kind == "head"
    assert review.baseline.ref == "HEAD"
    assert review.baseline.commit == head

    # 基线行（round 0 / head），且**没有**替用户仓库写任何提交/tag（AC-002-5）
    baselines = await _baselines_of(db_session, session_id)
    assert [(row.round, row.kind, row.ref, row.commit) for row in baselines] == [
        (0, "head", "HEAD", head)
    ]
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(root, "tag", "--list") == ""

    by_path = {item.path: item for item in review.files}
    assert by_path["src/deep.py"].change == "added"
    assert by_path["中文.txt"].change == "added"
    assert by_path["space name.md"].change == "added"
    assert by_path["empty_new.txt"].change == "added"
    assert by_path["empty_new.txt"].additions == 0
    assert by_path["bin.dat"].binary is True
    assert by_path["doc.md"].change == "modified"
    assert by_path["remove.md"].change == "deleted"
    assert by_path["gone.bin"].change == "deleted"
    assert by_path["gone.bin"].binary is True
    assert by_path["dir with space/new.txt"].change == "renamed"
    assert by_path["dir with space/new.txt"].old_path == "dir with space/old.txt"
    assert review.additions_total == sum(item.additions for item in review.files)
    assert review.deletions_total == sum(item.deletions for item in review.files)
    assert review.additions_total > 0 and review.deletions_total > 0

    # 事件：diff_ready 过契约校验，patch_ref 指向详情端点
    ready_rows = await _events_of_type(db_session, session_id, "diff_ready")
    assert len(ready_rows) == 1
    event = ready_rows[0]
    payload = event_payload([event], "diff_ready")[0]
    assert payload["review_id"] == review.id
    assert payload["round"] == 1
    assert payload["patch_ref"] == f"{REVIEWS}/{review.id}"
    assert payload["additions_total"] == review.additions_total
    assert payload["deletions_total"] == review.deletions_total
    # 事件载荷能被契约模型反序列化（totals 求和强校验在模型 validator 里）
    rebuilt = DiffReadyEvent.model_validate(
        {
            **payload,
            "seq": event.seq,
            "session_id": str(event.session_id),
            "run_id": str(event.run_id) if event.run_id else None,
            "agent": event.agent,
            "created_at": event.created_at,
        }
    )
    assert len(rebuilt.files) == len(review.files)


async def test_prepare_review_without_changes_creates_nothing(
    diff_env, async_client, db_session
) -> None:
    """工作区与基线一致 → 不建行、不发事件（D6：没变化就没有审查轮）。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    _seed_repo(diff_env.workspace(session_id))

    assert await _prepare(diff_env, session_id) is None

    assert await _reviews_of(db_session, session_id) == []
    assert await _events_of_type(db_session, session_id, "diff_ready") == []
    # 基线仍被记录（round 0），但工作区没有残留暂存（无变化分支 reset）
    assert [row.round for row in await _baselines_of(db_session, session_id)] == [0]
    assert _git(diff_env.workspace(session_id), "diff", "--cached", "--name-only") == ""


# --------------------------------------------------------------------------- 非 git 输入


async def test_prepare_review_non_git_input_uses_init_baseline(
    diff_env, async_client, db_session
) -> None:
    """非 git 输入：round-0 兜底基线（init 提交 + tag），首轮 diff 对照它（AC-002-5）。"""
    detail = await create_session(async_client, title="非 git 输入")
    await db_session.commit()
    sandbox = diff_env.manager.create(detail.id)
    root = diff_env.workspace(detail.id)

    # 首次 prepare 只建 round-0 兜底基线（init + 空提交 + tag）；此时无变化 → None
    assert await _prepare(diff_env, detail.id) is None
    (root / "hello.txt").write_text("hello\n", encoding="utf-8")

    review = await _prepare(diff_env, detail.id)

    assert review is not None
    assert review.baseline.kind == "init"
    assert review.baseline.ref == "lcode-baseline/round-0"
    assert review.baseline.commit == _git(root, "rev-parse", "HEAD")
    assert _git(root, "log", "-1", "--pretty=%s") == "[lcode-baseline] init"
    assert "lcode-baseline/round-0" in _git(root, "tag", "--list").splitlines()
    assert sandbox.state.created is True


# --------------------------------------------------------------------------- 2. supersede


async def test_new_round_supersedes_pending_review(diff_env, async_client, db_session) -> None:
    """pending 存在时新轮取代旧轮：旧行 rejected(system) + feedback，事件转 diff_resolved。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None

    (root / "two.txt").write_text("2\n", encoding="utf-8")
    second = await _prepare(diff_env, session_id)
    assert second is not None
    assert (first.round, second.round) == (1, 2)

    rows = await _reviews_of(db_session, session_id)
    old, new = rows
    assert old.verdict == "rejected"
    assert old.decided_by == "system"
    assert old.feedback == "已被第 2 轮审查取代"
    assert old.resolved_at is not None
    assert new.verdict == "pending"

    resolved = await _events_of_type(db_session, session_id, "diff_resolved")
    assert len(resolved) == 1
    payload = event_payload(resolved, "diff_resolved")[0]
    assert payload["review_id"] == first.id
    assert payload["round"] == 1
    assert payload["verdict"] == "rejected"
    assert payload["decided_by"] == "system"
    assert payload["feedback"] == "已被第 2 轮审查取代"
    DiffResolvedEvent.model_validate(
        {
            **payload,
            "seq": resolved[0].seq,
            "session_id": str(resolved[0].session_id),
            "run_id": None,
            "agent": None,
            "created_at": resolved[0].created_at,
        }
    )
    # 顺序：第 1 轮 ready → 第 2 轮 supersede resolved → 第 2 轮 ready
    ready = await _events_of_type(db_session, session_id, "diff_ready")
    assert len(ready) == 2
    assert resolved[0].seq > ready[0].seq
    assert resolved[0].seq < ready[1].seq


# --------------------------------------------------------------------------- 3. 裁决与基线链


async def test_accepted_advances_baseline_and_next_round_uses_it(
    diff_env, async_client, db_session
) -> None:
    """accepted → commit+tag round-N + git_baselines(accepted)；下一轮对照新基线。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    head = _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None

    response = await async_client.post(
        f"{REVIEWS}/{first.id}/verdict", json={"verdict": "accepted"}
    )
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "accepted"
    assert detail.decided_by == "user"
    assert detail.resolved_at is not None

    accepted_sha = _git(root, "rev-parse", "HEAD")
    assert accepted_sha != head
    assert _git(root, "log", "-1", "--pretty=%s").startswith("[lcode-baseline] round-1")
    assert "lcode-baseline/round-1" in _git(root, "tag", "--list").splitlines()

    baselines = await _baselines_of(db_session, session_id)
    assert [(row.round, row.kind, row.ref, row.commit) for row in baselines] == [
        (0, "head", "HEAD", head),
        (1, "accepted", "lcode-baseline/round-1", accepted_sha),
    ]
    resolved = await _events_of_type(db_session, session_id, "diff_resolved")
    assert event_payload(resolved, "diff_resolved")[0]["verdict"] == "accepted"

    # 新一轮：对照 accepted 基线（commit = accepted_sha），round 2
    (root / "two.txt").write_text("2\n", encoding="utf-8")
    second = await _prepare(diff_env, session_id)
    assert second is not None
    assert second.round == 2
    assert second.baseline.kind == "accepted"
    assert second.baseline.ref == "lcode-baseline/round-1"
    assert second.baseline.commit == accepted_sha
    assert [item.path for item in second.files] == ["two.txt"]


async def test_rejected_keeps_baseline_and_worktree_editable(
    diff_env, async_client, db_session
) -> None:
    """rejected：不推进基线、保存 feedback、`reset` 撤销暂存（工作区仍可编辑）。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    head = _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None

    response = await async_client.post(
        f"{REVIEWS}/{first.id}/verdict",
        json={"verdict": "rejected", "feedback": "这里应改成两行"},
    )
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "rejected"
    assert (detail.feedback or "").startswith("这里应改成两行")

    assert _git(root, "rev-parse", "HEAD") == head
    assert "lcode-baseline/round-1" not in _git(root, "tag", "--list")
    baselines = await _baselines_of(db_session, session_id)
    assert [row.round for row in baselines] == [0]
    # 暂存已撤销：one.txt 回到 untracked（`??`），不是 staged `A `
    status = _git(root, "status", "--porcelain")
    assert "?? one.txt" in status

    # 下一轮仍对照同一基线（round 2，baseline.commit == head）
    second = await _prepare(diff_env, session_id)
    assert second is not None
    assert second.round == 2
    assert second.baseline.commit == head


async def test_accept_losing_race_to_supersede_writes_no_git(
    diff_env, async_client, db_session, monkeypatch
) -> None:
    """supersede 抢在 accept 的「读行 → 抢锁」之间：409 且**不做任何 git 写**。

    旧实现（先 git 后抢锁）会留下一份用**最新索引**（而不是本轮固化快照）创建的
    `[lcode-baseline] round-N` 提交 + tag，随后 accept 得 409 —— 基线已脏，下一轮还可能
    因 "nothing to commit" 恒 500。现在 git 推进在行锁内，锁内复核发现已被取代就 409。
    """
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None
    head_before = _git(root, "rev-parse", "HEAD")
    tags_before = _git(root, "tag", "--list")

    original_lock = DiffService._lock_pending

    async def racing_lock(self, key, review_id):
        # 抢锁前一刻让「新 run 收口」抢先 supersede 本轮（模拟 TOCTOU 窗口）
        (root / "two.txt").write_text("2\n", encoding="utf-8")
        superseder = await _prepare(diff_env, session_id)
        assert superseder is not None and superseder.round == 2
        return await original_lock(self, key, review_id)

    monkeypatch.setattr(DiffService, "_lock_pending", racing_lock)

    response = await async_client.post(
        f"{REVIEWS}/{first.id}/verdict", json={"verdict": "accepted"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIFF_ALREADY_RESOLVED"

    # 独立 git 进程核对：无新提交、无 round-1 tag（脏基线是这条修复的核心防范对象）
    assert _git(root, "rev-parse", "HEAD") == head_before
    assert _git(root, "tag", "--list") == tags_before
    # 幂等表：git_baselines 无新行（只有 round-0 head）
    assert [row.round for row in await _baselines_of(db_session, session_id)] == [0]
    # 该轮仍是「被取代」而不是 accepted
    assert [row.verdict for row in await _reviews_of(db_session, session_id)] == [
        "rejected",
        "pending",
    ]


async def test_accept_commits_round_snapshot_not_later_edits(
    diff_env, async_client, db_session
) -> None:
    """accept 提交**本轮固化快照**（索引内容），不是 accept 时刻的工作区内容（D6）。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None
    # 本轮审查之后又被改动（未暂存）：不属于本轮审查内容，绝不能混进基线
    (root / "one.txt").write_text("2\n", encoding="utf-8")

    response = await async_client.post(
        f"{REVIEWS}/{first.id}/verdict", json={"verdict": "accepted"}
    )
    assert response.status_code == 200

    assert _git(root, "show", "HEAD:one.txt") == "1"  # 基线里是 round-1 的快照内容
    assert (root / "one.txt").read_text(encoding="utf-8") == "2\n"  # 后续改动留在工作区


async def test_accept_without_staged_changes_reuses_head(
    diff_env, async_client, db_session
) -> None:
    """改动在裁决前被撤回（索引与 HEAD 一致）→ 不建空提交，HEAD 记作本轮基线。

    旧行为：`git commit` "nothing to commit" 非零退出 → 500，该轮永远 pending。现在
    accepted 成立、tag 落在 HEAD（ref/commit 自洽），并记 WARNING（服务侧日志）。
    """
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    head = _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    first = await _prepare(diff_env, session_id)
    assert first is not None

    # 用户把改动撤回了；下一次收口无变化 → 不建新轮，并把暂存 reset 掉
    (root / "one.txt").unlink()
    assert await _prepare(diff_env, session_id) is None

    response = await async_client.post(
        f"{REVIEWS}/{first.id}/verdict", json={"verdict": "accepted"}
    )
    assert response.status_code == 200
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "accepted"

    # 没有新提交；tag 仍落在 HEAD 上（基线 ref 与 commit 自洽）
    assert _git(root, "rev-parse", "HEAD") == head
    assert "lcode-baseline/round-1" in _git(root, "tag", "--list").splitlines()
    assert _git(root, "rev-parse", "lcode-baseline/round-1") == head
    baselines = await _baselines_of(db_session, session_id)
    assert [(row.round, row.kind, row.ref, row.commit) for row in baselines] == [
        (0, "head", "HEAD", head),
        (1, "accepted", "lcode-baseline/round-1", head),
    ]


# --------------------------------------------------------------------------- 4. 三端点


async def test_diff_endpoints_detail_file_and_errors(diff_env, async_client, db_session) -> None:
    """详情 / 逐文件 patch（子目录 + 空格路径）/ 三类错误码。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    _mutate_workspace(root)
    review = await _prepare(diff_env, session_id)
    assert review is not None

    detail_response = await async_client.get(f"{REVIEWS}/{review.id}")
    detail = DiffReviewDetail.model_validate(unwrap(detail_response))
    assert detail.id == review.id
    assert detail.files == review.files

    nested = "src/deep.py"
    file_response = await async_client.get(f"{REVIEWS}/{review.id}/files/{quote(nested, safe='/')}")
    file_detail = DiffFileDetail.model_validate(unwrap(file_response))
    assert file_detail.path == nested
    assert file_detail.change == "added"
    assert file_detail.patch is not None and "print('hi')" in file_detail.patch

    spaced = "dir with space/new.txt"
    spaced_response = await async_client.get(
        f"{REVIEWS}/{review.id}/files/{quote(spaced, safe='/')}"
    )
    spaced_detail = DiffFileDetail.model_validate(unwrap(spaced_response))
    assert spaced_detail.change == "renamed"
    assert spaced_detail.patch is not None
    assert "rename from dir with space/old.txt" in spaced_detail.patch

    # rename 的旧路径同样可寻址（返回条目的 path 恒为新路径）
    old_response = await async_client.get(
        f"{REVIEWS}/{review.id}/files/{quote('dir with space/old.txt', safe='/')}"
    )
    assert DiffFileDetail.model_validate(unwrap(old_response)).path == spaced

    missing_file = await async_client.get(f"{REVIEWS}/{review.id}/files/nope.txt")
    assert missing_file.status_code == 404
    assert missing_file.json()["code"] == "NOT_FOUND"

    unknown = await async_client.get(f"{REVIEWS}/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "DIFF_REVIEW_NOT_FOUND"

    invalid = await async_client.get(f"{REVIEWS}/not-a-uuid")
    assert invalid.status_code == 404
    assert invalid.json()["code"] == "DIFF_REVIEW_NOT_FOUND"

    first = await async_client.post(f"{REVIEWS}/{review.id}/verdict", json={"verdict": "accepted"})
    assert first.status_code == 200
    again = await async_client.post(f"{REVIEWS}/{review.id}/verdict", json={"verdict": "rejected"})
    assert again.status_code == 409
    assert again.json()["code"] == "DIFF_ALREADY_RESOLVED"


async def test_diff_file_binary_and_oversize_patch(diff_env, async_client, db_session) -> None:
    """binary → patch=null + blob_ref；>1MB 文本 → 截断（patch=null + truncated + blob_ref）。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    big_line = "x" * 99
    (root / "big.txt").write_text("".join(f"{big_line}\n" for _ in range(12000)), encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    binary_detail = DiffFileDetail.model_validate(
        unwrap(await async_client.get(f"{REVIEWS}/{review.id}/files/bin.dat"))
    )
    assert binary_detail.binary is True
    assert binary_detail.patch is None
    assert binary_detail.blob_ref is not None and binary_detail.blob_ref.startswith("sandbox:")

    big_detail = DiffFileDetail.model_validate(
        unwrap(await async_client.get(f"{REVIEWS}/{review.id}/files/big.txt"))
    )
    assert big_detail.binary is False
    assert big_detail.patch is None
    assert big_detail.blob_ref is not None

    row = await db_session.scalar(select(DiffReview).where(DiffReview.id == uuid.UUID(review.id)))
    assert row is not None
    entries = {entry["path"]: entry for entry in row.files}
    assert entries["big.txt"]["truncated"] is True
    assert entries["big.txt"]["patch"] is None
    assert entries["big.txt"]["patch_bytes"] > 1024 * 1024
    # totals 仍与 files 求和一致（截断不影响行数统计）
    assert review.additions_total == sum(item.additions for item in review.files)


async def test_oversize_estimate_skips_patch_and_marks_files(
    diff_env, async_client, db_session
) -> None:
    """numstat 预估超单文件上限 → **不拉 patch**（jsonb 标 `patch_skipped=oversize`）。

    与上一个用例互补：那条走「读后截断」兜底（预判通过、实际超限），这条走预判
    （20000 行 × 64B/行 ≈ 1.28MB > 1MB，`git diff` 根本不会执行）。
    """
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "many_lines.txt").write_text("".join("x\n" for _ in range(20000)), encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None
    assert review.additions_total == sum(item.additions for item in review.files)

    row = await db_session.scalar(select(DiffReview).where(DiffReview.id == uuid.UUID(review.id)))
    assert row is not None
    entries = {entry["path"]: entry for entry in row.files}
    entry = entries["many_lines.txt"]
    assert entry["patch"] is None
    assert entry["truncated"] is True
    assert entry["patch_skipped"] == "oversize"
    assert entry["estimated_patch_bytes"] == 20000 * 64
    assert "patch_bytes" not in entry  # 没读过正文，不报实际字节
    assert entry["blob_ref"].startswith("sandbox:")

    detail = DiffFileDetail.model_validate(
        unwrap(await async_client.get(f"{REVIEWS}/{review.id}/files/many_lines.txt"))
    )
    assert detail.patch is None
    assert detail.blob_ref is not None


# --------------------------------------------------------------------------- 5. 驳回返工


async def test_rejected_with_feedback_starts_rework_run(diff_env, async_client, db_session) -> None:
    """rejected + feedback → 复用消息通道起新 run，首条用户消息含反馈原文。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    diff_env.set_model(ScriptedChatModel(steps=scripts.simple_reply()))
    response = await async_client.post(
        f"{REVIEWS}/{review.id}/verdict",
        json={"verdict": "rejected", "feedback": "请把实现改成两行"},
    )
    assert response.status_code == 200

    await wait_run_state(async_client, session_id, "idle")
    run = await latest_run(db_session, session_id)
    assert run.status == "completed"
    rows = list(
        await db_session.scalars(
            select(MessageRow)
            .where(MessageRow.session_id == uuid.UUID(session_id), MessageRow.role == "user")
            .order_by(MessageRow.created_at)
        )
    )
    assert len(rows) == 1
    assert "上一轮改动未通过 diff 审查" in rows[0].content
    assert "请把实现改成两行" in rows[0].content


async def test_reject_while_run_active_records_note_without_new_run(
    diff_env, async_client, db_session
) -> None:
    """会话已有活动 run：只记录裁决 + 系统提示，不建 run（m3-plan 风险 5）。

    注记必须在 `diff_resolved` 发射**之前**就进 feedback（事件不可变）：否则多标签页 /
    刷新回放看到的是「没有注记」的旧事件，用户会误以为返工在进行中。故这里同时断言
    事件与 GET 详情都含注记，且两者逐字一致。
    """
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    active = RunRow(
        session_id=uuid.UUID(session_id),
        status="running",
        started_at=datetime.now(UTC),
    )
    db_session.add(active)
    await db_session.commit()

    response = await async_client.post(
        f"{REVIEWS}/{review.id}/verdict",
        json={"verdict": "rejected", "feedback": "再检查一下"},
    )
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "rejected"
    assert "再检查一下" in (detail.feedback or "")
    assert "返工未自动启动" in (detail.feedback or "")

    # 事件侧：注记随 diff_resolved 一起发射（不是事后补写）
    resolved = await _events_of_type(db_session, session_id, "diff_resolved")
    assert len(resolved) == 1
    payload = event_payload(resolved, "diff_resolved")[0]
    assert payload["verdict"] == "rejected"
    assert payload["decided_by"] == "user"
    assert "返工未自动启动" in (payload["feedback"] or "")
    assert payload["feedback"] == detail.feedback

    # GET 详情与裁决响应同一份事实（刷新/多标签页一致）
    fetched = DiffReviewDetail.model_validate(
        unwrap(await async_client.get(f"{REVIEWS}/{review.id}"))
    )
    assert fetched.feedback == detail.feedback

    runs = list(
        await db_session.scalars(select(RunRow).where(RunRow.session_id == uuid.UUID(session_id)))
    )
    assert len(runs) == 1  # 只有测试预置的活动 run，没有新建返工 run


# --------------------------------------------------------------------------- 6. 失败隔离


async def test_diff_failure_does_not_fail_run(
    diff_env, async_client, db_session, monkeypatch
) -> None:
    """diff 计算抛错 → run 仍 completed（无 diff_ready）；产物失败不改写运行终态。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)

    async def _boom(**_kwargs):
        raise RuntimeError("diff 计算炸了")

    monkeypatch.setattr(diff_service_module, "prepare_review_short_tx", _boom)
    diff_env.set_model(ScriptedChatModel(steps=scripts.simple_reply()))

    response = await post_message(async_client, session_id, "你好")
    assert response.status_code == 200
    await wait_run_state(async_client, session_id, "idle")

    run = await latest_run(db_session, session_id)
    assert run.status == "completed"
    assert await _events_of_type(db_session, session_id, "diff_ready") == []
    assert await _reviews_of(db_session, session_id) == []


# --------------------------------------------------------------------------- 7. 降级与副作用隔离


async def test_reject_after_sandbox_released_still_records_verdict(
    diff_env, async_client, db_session
) -> None:
    """沙箱已释放：`git reset` 跳过（只记 warning），裁决本身照常成立。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    diff_env.manager.release(session_id, reason="test")

    response = await async_client.post(
        f"{REVIEWS}/{review.id}/verdict",
        json={"verdict": "rejected", "feedback": "沙箱不在了也要能记"},
    )
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "rejected"
    assert detail.feedback == "沙箱不在了也要能记"
    # 驳回自动返工已在后台起 run：等它收敛（不在测试收尾时丢下运行中的任务）
    await wait_run_state(async_client, session_id, "idle")


async def test_rework_start_failure_keeps_verdict(
    diff_env, async_client, db_session, monkeypatch
) -> None:
    """返工启动失败（非冲突）：只记 ERROR，裁决已成立、feedback 不被系统注记改写。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    async def _boom(self, *_args, **_kwargs):
        raise RuntimeError("消息通道炸了")

    monkeypatch.setattr(MessageService, "post_message", _boom)
    response = await async_client.post(
        f"{REVIEWS}/{review.id}/verdict",
        json={"verdict": "rejected", "feedback": "返工失败也要保留意见"},
    )
    detail = DiffReviewDetail.model_validate(unwrap(response))
    assert detail.verdict == "rejected"
    assert detail.feedback == "返工失败也要保留意见"


async def test_verdict_without_rework_starter_only_logs(diff_env, async_client, db_session) -> None:
    """未注入返工启动器（直连 service 的装配形态）：裁决落库，不建 run。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    service = DiffService(db_session, pipeline=get_event_pipeline(), manager=diff_env.manager)
    detail = await service.verdict(
        review.id, DiffVerdictRequest(verdict="rejected", feedback="直连 service")
    )

    assert detail.verdict == "rejected"
    runs = list(
        await db_session.scalars(select(RunRow).where(RunRow.session_id == uuid.UUID(session_id)))
    )
    assert runs == []


async def test_reset_failure_after_reject_is_isolated(
    diff_env, async_client, db_session, monkeypatch
) -> None:
    """`git reset` 失败（工作区仍可编辑，仅暂存态残留）：只记 warning，裁决照常成功。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    (root / "one.txt").write_text("1\n", encoding="utf-8")
    review = await _prepare(diff_env, session_id)
    assert review is not None

    def _boom(self) -> None:
        raise SandboxError("reset 失败")

    monkeypatch.setattr(GitWorkspace, "unstage_all", _boom)
    response = await async_client.post(
        f"{REVIEWS}/{review.id}/verdict", json={"verdict": "rejected", "feedback": "x"}
    )
    assert response.status_code == 200
    assert DiffReviewDetail.model_validate(unwrap(response)).verdict == "rejected"
    await wait_run_state(async_client, session_id, "idle")


async def test_ensure_baseline_requires_live_sandbox(async_client, db_session) -> None:
    """基线链的降级边界：无管理器 / 无在册沙箱 / 非法 id 都明确报错（不造假基线）。"""
    detail = await create_session(async_client, title="无沙箱")
    await db_session.commit()

    no_manager = DiffService(db_session, pipeline=get_event_pipeline(), manager=None)
    with pytest.raises(GitWorkspaceError):
        await no_manager.ensure_baseline(detail.id)

    def _never_create(provider: str, session_id: str) -> FakeSandbox:
        raise AssertionError(f"不应创建沙箱：{provider}/{session_id}")

    empty_manager = SandboxManager(factory=_never_create)
    service = DiffService(db_session, pipeline=get_event_pipeline(), manager=empty_manager)
    with pytest.raises(GitWorkspaceError):
        await service.ensure_baseline(detail.id)

    with pytest.raises(InvalidArgument):
        await service.ensure_baseline("not-a-uuid")


async def test_prepare_review_resumes_paused_sandbox(diff_env, async_client, db_session) -> None:
    """沙箱空闲 pause 后再收口：先 resume 再算 diff（不因暂停丢失审查）。"""
    session_id = await _create_session_with_sandbox(diff_env, async_client, db_session)
    root = diff_env.workspace(session_id)
    _seed_repo(root)
    assert await _prepare(diff_env, session_id) is None  # 只建基线

    diff_env.manager.pause(session_id, reason="idle")
    assert diff_env.manager.stable_phase(session_id) == "paused"
    (root / "one.txt").write_text("1\n", encoding="utf-8")

    review = await _prepare(diff_env, session_id)
    assert review is not None
    assert review.round == 1
    assert diff_env.manager.stable_phase(session_id) == "running"
