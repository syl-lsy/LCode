"""diff 审查域服务（M3 Phase 4，REQ-007 / AC-007-1~5）：基线链、轮次、裁决、返工。

职责边界（D6/D7 的落地）：

- **基线链**（`ensure_baseline`）：读 `git_baselines` 最近一条；没有则按 D7 记一条
  round-0——git 输入（有 HEAD）记 `kind=head` 且**不做任何 git 写**；非 git 输入（或空
  仓库）走 round-0 兜底基线（`[lcode-baseline] init` 提交 + `lcode-baseline/round-0`
  tag，`kind=init`，AC-002-5）；
- **就绪**（`prepare_review`，run 正常收口时调用）：工作区无变化不发事件不建行（D6）；
  `round = 会话内最大审查轮 + 1`；对照基线 = 最近一条 `git_baselines`；已有 pending 审查
  时旧轮置 `rejected`/`decided_by=system`/`feedback=已被第 N 轮审查取代` 并发
  `diff_resolved`（旧轮要被客户端关掉）；patch 在 `diff_ready` 时刻固化进
  `diff_reviews.files` jsonb（之后工作区再变不影响本轮审查内容）；**patch 拉取有三层
  保护**（单文件超过 1MB 不拉/截断 + 单轮总量与文件数预算，见 `_file_payload` 与
  `app.sandbox.git.collect_diff`，m3-plan 风险 4）；
- **裁决**（`verdict`）：accepted → **先抢行锁并复核仍 pending**，锁内推进基线（沙箱内
  commit + tag `lcode-baseline/round-N`）→ 锁内复核裁决未被并发改写 → 落行/写
  `git_baselines(accepted)`/commit → 发 `diff_resolved`（AC-007-4）。顺序是正确性优先：
  旧实现先跑 git 再抢锁，supersede 若插在两者之间会留下用**最新索引**（而非本轮固化
  快照）创建的基线提交与 tag，且下一轮可能因 "nothing to commit" 恒 500。代价是持锁
  期间做一次沙箱 git 往返（数十到数百毫秒；P0 单用户可接受）；rejected → **不推进
  基线**、保存 feedback、发 `diff_resolved`，并让工作区保持可编辑（`git reset` 撤销
  diff 前的暂存，spike §4）；已 resolved 的重复裁决 → 409 `DIFF_ALREADY_RESOLVED`；
  未知/非法 id → 404 `DIFF_REVIEW_NOT_FOUND`；
- **驳回自动返工**：rejected + 非空 feedback → 复用「发送消息」通道起新 run（feedback
  作为用户消息注入，措辞让模型理解为「上一轮改动被驳回，原因如下」）；会话已有活动 run
  时（`RUN_ALREADY_ACTIVE`）**只记录裁决与提示，不建 run**（m3-plan 风险 5）——提示以
  系统注记追加到该轮 `feedback`（契约零变更下唯一能被前端展示的字段），**并且在落行 +
  发 `diff_resolved` 之前就写入**：事件一经发射不可变，注记若在 emit 之后才追加，多标签页
  与刷新回放都会看到「没有注记」的旧事件、误以为返工在进行中。残余窗口见 `verdict`。

事务口径（A3 §7.3）：run 收口的 prepare 走**自持短事务**（`get_session_factory`，git 命令
在两次短事务之间执行，不占连接）；请求路径（get/get_file/verdict）用注入的请求级 `db`，
verdict 在 emit 前显式 commit——事件永不引用未提交的业务事实。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_session_factory
from app.core.logger import BusinessLog
from app.events import EventPipeline
from app.exception.common import InvalidArgument
from app.exception.diff import DiffAlreadyResolved, DiffFileNotFound, DiffReviewNotFound
from app.exception.run import RunAlreadyActive
from app.models import DiffReview, GitBaseline, Run
from app.sandbox.base import LcodeSandbox, SandboxError
from app.sandbox.git import (
    PATCH_LIMIT_BYTES,
    BaselineCommit,
    GitWorkspace,
    GitWorkspaceError,
    PatchBudget,
    WorkspaceFileDiff,
)
from app.sandbox.manager import SandboxManager
from app.services.base import BaseService
from app.services.message_service import ACTIVE_RUN_STATUSES
from lcode_contracts.baseline import check_commit_sha
from lcode_contracts.events import (
    BaselineRef,
    DiffFileSummary,
    DiffReadyEvent,
    DiffResolvedEvent,
)
from lcode_contracts.rest import DiffFileDetail, DiffReviewDetail, DiffVerdictRequest

DECIDED_BY_USER: str = "user"
DECIDED_BY_SYSTEM: str = "system"

VERDICT_PENDING: str = "pending"

SYSTEM_NOTE_PREFIX: str = "[系统] "
"""系统注记前缀（与用户意见拼在同一 feedback 字段时用于视觉区分）。"""

SUPERSEDED_FEEDBACK_TEMPLATE: str = "已被第 {round} 轮审查取代"
"""旧轮被新轮取代时的系统反馈（D6 原文口径）。"""

REWORK_NOT_STARTED_NOTE: str = "返工未自动启动：会话已有运行中的任务。"
"""RUN_ALREADY_ACTIVE 冲突时追加到 feedback 的系统注记（m3-plan 风险 5）。"""

BaselineKind = Literal["head", "init", "accepted"]

ReworkStarter = Callable[[str, str], Awaitable[Any]]
"""返工启动器：`(session_id, content) → ack`（实现 = `MessageService.post_message`）。"""


@dataclass(frozen=True, slots=True)
class BaselineSnapshot:
    """一次 diff 对照基线（`git_baselines` 行 / 即将落行的行）。"""

    kind: BaselineKind
    ref: str
    commit: str
    round: int

    def to_ref(self) -> BaselineRef:
        """契约 `BaselineRef`（事件里携带的对照点）。"""
        return BaselineRef(kind=self.kind, ref=self.ref, commit=self.commit)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_rework_message(*, round_: int, feedback: str) -> str:
    """驳回返工的用户消息（措辞锚：让模型明确「上一轮改动被驳回 + 原因」）。"""
    reason = feedback.strip()
    if reason:
        return (
            f"（系统提示：上一轮改动未通过 diff 审查，第 {round_} 轮审查被驳回。）\n"
            f"驳回意见如下：\n\n{reason}\n\n"
            "请根据以上意见修改工作区；完成后我会重新审查 diff。"
        )
    return (
        f"（系统提示：上一轮改动未通过 diff 审查，第 {round_} 轮审查被驳回，但未给出具体意见。）\n"
        "请检查上一轮改动的正确性与完整性并继续修改；完成后我会重新审查 diff。"
    )


def _with_system_note(feedback: str | None, note: str | None) -> str | None:
    """把系统注记追加到用户意见尾部（`[系统]` 前缀 + 空行分隔）；无注记原样返回。"""
    if note is None:
        return feedback
    body = (feedback or "").rstrip()
    line = f"{SYSTEM_NOTE_PREFIX}{note}"
    return f"{body}\n\n{line}" if body else line


# --------------------------------------------------------------------------- 行投影


def _file_summary(entry: dict[str, Any]) -> DiffFileSummary:
    return DiffFileSummary.model_validate(
        {
            "path": entry["path"],
            "old_path": entry.get("old_path"),
            "change": entry["change"],
            "additions": int(entry.get("additions", 0)),
            "deletions": int(entry.get("deletions", 0)),
            "binary": bool(entry.get("binary", False)),
        }
    )


def _to_detail(row: DiffReview) -> DiffReviewDetail:
    """ORM 行 → 契约 `DiffReviewDetail`（files 从内部超集投影为摘要）。"""
    return DiffReviewDetail.model_validate(
        {
            "id": str(row.id),
            "session_id": str(row.session_id),
            "run_id": str(row.run_id) if row.run_id is not None else None,
            "round": row.round,
            "verdict": row.verdict,
            "baseline": {
                "kind": row.baseline_kind,
                "ref": row.baseline_ref,
                "commit": row.baseline_commit,
            },
            "files": [_file_summary(entry) for entry in (row.files or [])],
            "additions_total": row.additions_total,
            "deletions_total": row.deletions_total,
            "feedback": row.feedback,
            "decided_by": row.decided_by,
            "created_at": row.created_at,
            "resolved_at": row.resolved_at,
        }
    )


def _blob_ref(*, root: str, entry: WorkspaceFileDiff, baseline_commit: str) -> str:
    """binary / 截断文件的正文引用（契约 `DiffFileDetail.blob_ref`）。

    形态（非 wire 语义，仅定位用）：已删除文件指向基线提交里的 git blob
    ``git:<commit>:<path>``；其余指向工作区里的沙箱绝对路径 ``sandbox:<abs path>``。
    """
    if entry.change == "deleted":
        return f"git:{baseline_commit}:{entry.path}"
    return f"sandbox:{root.rstrip('/')}/{entry.path}"


def _file_payload(entry: WorkspaceFileDiff, *, root: str, baseline_commit: str) -> dict[str, Any]:
    """单文件 → `diff_reviews.files` 的内部超集条目（含 patch / blob_ref / 跳过标记）。

    `patch=None` 的三种来源在 jsonb 里可区分：binary（`binary=True`）、**未拉取**
    （`patch_skipped=oversize|budget` + `estimated_patch_bytes`）、**读后截断**
    （`truncated=True` + `patch_bytes` 实际字节）。最后一种是兜底（numstat 预判失效：
    单行超长、二进制伪装），确保任何路径都不会把 >1MB 正文落库。
    """
    payload: dict[str, Any] = {
        "path": entry.path,
        "old_path": entry.old_path,
        "change": entry.change,
        "additions": entry.additions,
        "deletions": entry.deletions,
        "binary": entry.binary,
    }
    if entry.binary:
        payload["patch"] = None
        payload["blob_ref"] = _blob_ref(root=root, entry=entry, baseline_commit=baseline_commit)
        return payload
    if entry.patch_skipped is not None:
        # 未拉取（numstat 预判超限 / 单轮预算耗尽）：无正文，只有 blob_ref 与量级估计
        payload["patch"] = None
        payload["truncated"] = True
        payload["patch_skipped"] = entry.patch_skipped
        if entry.estimated_patch_bytes is not None:
            payload["estimated_patch_bytes"] = entry.estimated_patch_bytes
        payload["blob_ref"] = _blob_ref(root=root, entry=entry, baseline_commit=baseline_commit)
        return payload
    patch = entry.patch or ""
    size = len(patch.encode("utf-8"))
    if size > PATCH_LIMIT_BYTES:
        # 截断：正文不落库（REST 返回 patch=null + blob_ref）；体积留痕便于排查
        payload["patch"] = None
        payload["truncated"] = True
        payload["patch_bytes"] = size
        payload["blob_ref"] = _blob_ref(root=root, entry=entry, baseline_commit=baseline_commit)
        return payload
    payload["patch"] = patch
    return payload


# --------------------------------------------------------------------------- 服务


class DiffService(BaseService):
    """diff 审查域业务：基线链 / 就绪 / 详情 / 逐文件 patch / 裁决（+返工）。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        pipeline: EventPipeline,
        manager: SandboxManager | None = None,
        rework_starter: ReworkStarter | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        workspace_dir: str | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(db)
        self._pipeline = pipeline
        self._manager = manager
        self._rework_starter = rework_starter
        self._session_factory = session_factory
        self._workspace_dir = workspace_dir
        self._now = now

    # ---------------------------------------------------------------- 基线链

    async def ensure_baseline(self, session_id: str, *, user_id: str = "local") -> BaselineSnapshot:
        """读最近一条基线；缺失则按 D7 记录 round-0（git 输入只读 / 非 git 走 init）。

        `kind=head` 分支**不做任何 git 写**（AC-002-5）：只 `rev-parse HEAD` 记录对照点。
        非 git 与空仓库走 round-0 兜底基线（`[lcode-baseline] init` + tag round-0）。

        S1 卸载口径（评审项 3）：`_live_client`（paused 时 resume 是 e2b connect 级别的
        秒级网络调用）与沙箱内 git 命令都是同步 SDK 往返，整体经 `asyncio.to_thread`
        在工作线程执行——本方法被 run 收口的 prepare_review 在主循环后台任务里调用，
        不卸载会阻塞全部 SSE 流与请求。
        """
        key = _uuid_of(session_id)
        factory = self._factory()
        async with factory() as session:
            existing = await latest_baseline(session, key)
            if existing is not None:
                return _snapshot_of(existing)

        if not self._has_provisioned_sandbox(session_id):
            # 懒加载：未 provision（本会话从未实际执行过工具）→ 无沙箱可对照，明确报错
            # 而不是经 `_baseline_probe` 的 git 命令触发 provision（收口车道不得触网）。
            # run 收口路径（prepare_review）已先行短路，本守卫面向直接调用方。
            raise GitWorkspaceError(
                "会话沙箱尚未 provision（本会话从未实际执行过工具），无法建立基线",
                detail=f"session={session_id}",
            )

        snapshot, note = await asyncio.to_thread(self._baseline_probe, session_id)

        async with factory() as session:
            # 并发/重入保护：两次收口同时补基线时只落一行（后到者复用先到者）
            again = await latest_baseline(session, key)
            if again is not None:
                return _snapshot_of(again)
            session.add(
                GitBaseline(
                    user_id=user_id,
                    session_id=key,
                    round=snapshot.round,
                    kind=snapshot.kind,
                    ref=snapshot.ref,
                    commit=snapshot.commit,
                    note=note,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # uq_git_baselines_session_round 兜底并发双写：回滚后复用已提交那行
                await session.rollback()
                again = await latest_baseline(session, key)
                if again is None:  # pragma: no cover —— 唯一约束冲突后行必存在
                    raise
                return _snapshot_of(again)
        return snapshot

    def _baseline_probe(self, session_id: str) -> tuple[BaselineSnapshot, str]:
        """（工作线程内执行）取会话沙箱并解析基线：git 输入只读 / 非 git 走 round-0 init。

        同步实现，**只允许经 `asyncio.to_thread` 调用**（S1 卸载口径）——`_live_client`
        的 paused→resume 与 `layout`/`ensure_init_baseline` 的沙箱 git 往返都是秒级
        网络调用；note 与快照成对返回（落 `git_baselines.note` 的文案随分支不同）。
        """
        client = self._live_client(session_id)
        workspace = self._workspace(client)
        layout = workspace.layout
        if layout.is_repo and layout.head is not None:
            return (
                BaselineSnapshot(kind="head", ref="HEAD", commit=layout.head, round=0),
                "git 输入：以输入仓库 HEAD 为起点（不做 git 写）",
            )
        commit, tag = workspace.ensure_init_baseline()
        return (
            BaselineSnapshot(kind="init", ref=tag, commit=commit, round=0),
            "非 git 输入：round-0 兜底基线提交",
        )

    # ---------------------------------------------------------------- 就绪（run 收口）

    async def prepare_review(
        self, *, session_id: str, run_id: str | None, user_id: str = "local"
    ) -> DiffReviewDetail | None:
        """run 正常收口时计算并固化本轮 diff（无变化 → None，不发事件、不建行）。

        工作流：ensure 基线 → 沙箱内 `add -A` + numstat/name-status + 逐文件 patch（无 DB
        连接）→ 短事务「取轮次 + supersede 旧 pending + 插新行」→ 逐条发 supersede 的
        `diff_resolved` → 发 `diff_ready`（事件只引用已提交事实）。

        逐文件 patch 走 `PatchBudget`（单文件预估上限 + 单轮总量/文件数预算）：超限文件
        不拉正文、只留 `blob_ref`，并在 jsonb 里标 `patch_skipped`；被跳过的数量记 WARNING
        （不静默——审查面板会少一些可内联展开的文件）。

        懒加载短路（bug 修复）：会话沙箱**未 provision**（纯聊天 run，从未实际使用）
        时整体短路返回 None——diff 只在发生过工具执行的 run 才有意义，且绝不在收口
        车道对未 provision 实例跑 git（那会经懒 provision 触发网络，形同虚设）。
        """
        if not self._has_provisioned_sandbox(session_id):
            return None
        snapshot = await self.ensure_baseline(session_id, user_id=user_id)
        # 沙箱 git 往返（add -A + numstat/name-status + 逐文件 patch）在工作线程执行（S1）
        file_diffs, workspace_root = await asyncio.to_thread(
            self._collect_diff_sync, session_id, snapshot.commit
        )
        if not file_diffs:
            return None

        self._log_patch_skips(session_id, file_diffs)
        files = [
            _file_payload(entry, root=workspace_root, baseline_commit=snapshot.commit)
            for entry in file_diffs
        ]
        additions_total = sum(int(entry["additions"]) for entry in files)
        deletions_total = sum(int(entry["deletions"]) for entry in files)

        key = _uuid_of(session_id)
        factory = self._factory()
        now = self._now()
        async with factory() as session:
            review, superseded = await self._persist_review(
                session,
                session_id=key,
                run_id=run_id,
                user_id=user_id,
                snapshot=snapshot,
                files=files,
                additions_total=additions_total,
                deletions_total=deletions_total,
                now=now,
            )
            await session.commit()
            detail = _to_detail(review)

        for old_id, old_round, old_run_id, old_session_id, feedback in superseded:
            try:
                await self._pipeline.emit(
                    DiffResolvedEvent(
                        seq=0,
                        session_id=old_session_id,
                        run_id=old_run_id,
                        agent=None,
                        created_at=now,
                        review_id=old_id,
                        round=old_round,
                        verdict="rejected",
                        feedback=feedback,
                        decided_by=DECIDED_BY_SYSTEM,
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 逐条隔离：一条回声失败不拖累新轮
                BusinessLog(
                    message="被取代审查轮的 diff_resolved 发射失败（旧轮已落库 rejected）",
                    action="准备diff审查",
                    entity="diff_review",
                    entity_id=old_id,
                ).error(exc)

        await self._pipeline.emit(
            DiffReadyEvent(
                seq=0,
                session_id=str(key),
                run_id=run_id,
                agent=None,
                created_at=now,
                review_id=detail.id,
                round=detail.round,
                baseline=snapshot.to_ref(),
                files=[_file_summary(entry) for entry in files],
                additions_total=additions_total,
                deletions_total=deletions_total,
                patch_ref=f"/api/v1/diff-reviews/{detail.id}",
            )
        )
        return detail

    def _collect_diff_sync(
        self, session_id: str, baseline_commit: str
    ) -> tuple[list[WorkspaceFileDiff], str]:
        """（工作线程内执行）resolve client → 解析 layout → `add -A` + 逐文件 patch 收集。

        同步实现，**只允许经 `asyncio.to_thread` 调用**（S1 卸载口径）；`root` 一并带回
        （`_file_payload` 需要，且属工作区派生值，避免回主循环后再触属性面）。
        """
        client = self._live_client(session_id)
        workspace = self._workspace(client)
        _ = workspace.layout  # 先解析一次（缓存；非仓库已在 ensure 阶段完成 init）
        file_diffs = workspace.collect_diff(
            baseline_commit, budget=PatchBudget(max_patch_bytes=PATCH_LIMIT_BYTES)
        )
        return file_diffs, workspace.root

    async def _persist_review(
        self,
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        run_id: str | None,
        user_id: str,
        snapshot: BaselineSnapshot,
        files: list[dict[str, Any]],
        additions_total: int,
        deletions_total: int,
        now: datetime,
    ) -> tuple[DiffReview, list[tuple[str, int, str | None, str, str]]]:
        """短事务内：取轮次 → supersede 旧 pending → 插新行（返回 superseded 事件载荷）。"""
        max_round = await session.scalar(
            select(func.max(DiffReview.round)).where(DiffReview.session_id == session_id)
        )
        next_round = int(max_round or 0) + 1
        superseded_feedback = SUPERSEDED_FEEDBACK_TEMPLATE.format(round=next_round)

        pending = list(
            await session.scalars(
                select(DiffReview)
                .where(DiffReview.session_id == session_id, DiffReview.verdict == VERDICT_PENDING)
                .order_by(DiffReview.round.asc())
            )
        )
        superseded: list[tuple[str, int, str | None, str, str]] = []
        for old in pending:
            old.verdict = "rejected"
            old.decided_by = DECIDED_BY_SYSTEM
            old.feedback = superseded_feedback
            old.resolved_at = now
            superseded.append(
                (
                    str(old.id),
                    old.round,
                    str(old.run_id) if old.run_id is not None else None,
                    str(old.session_id),
                    superseded_feedback,
                )
            )

        review = DiffReview(
            user_id=user_id,
            session_id=session_id,
            run_id=uuid.UUID(run_id) if run_id else None,
            round=next_round,
            verdict=VERDICT_PENDING,
            baseline_kind=snapshot.kind,
            baseline_ref=snapshot.ref,
            baseline_commit=snapshot.commit,
            files=files,
            additions_total=additions_total,
            deletions_total=deletions_total,
            created_at=now,
        )
        session.add(review)
        await session.flush()
        return review, superseded

    # ---------------------------------------------------------------- 查询

    async def get_review(self, review_id: str) -> DiffReviewDetail:
        """审查详情（404 `DIFF_REVIEW_NOT_FOUND`）。"""
        return _to_detail(await self._require_row(review_id))

    async def get_file(self, review_id: str, file_path: str) -> DiffFileDetail:
        """单文件完整 patch（`{file_path:path}` 路径参数含 `/`）。

        `file_path` 先按新路径匹配，rename 场景也接受旧路径（返回条目的 `path` 恒为新路径）。
        不在该轮 diff → 404 `NOT_FOUND`（`DiffFileNotFound`）；binary 与超限截断的 patch
        为 null，正文引用走 `blob_ref`。
        """
        row = await self._require_row(review_id)
        entries: list[dict[str, Any]] = list(row.files or [])
        entry = next((item for item in entries if item.get("path") == file_path), None)
        if entry is None:
            entry = next((item for item in entries if item.get("old_path") == file_path), None)
        if entry is None:
            raise DiffFileNotFound(
                f"文件不在该轮 diff 中：{file_path}", detail=f"review={review_id}"
            )
        return DiffFileDetail.model_validate(
            {
                "path": entry["path"],
                "change": entry["change"],
                "additions": int(entry.get("additions", 0)),
                "deletions": int(entry.get("deletions", 0)),
                "binary": bool(entry.get("binary", False)),
                "patch": entry.get("patch"),
                "blob_ref": entry.get("blob_ref"),
            }
        )

    # ---------------------------------------------------------------- 裁决

    async def verdict(self, review_id: str, payload: DiffVerdictRequest) -> DiffReviewDetail:
        """一次裁决（AC-007-3/4）：accepted 推进基线；rejected 保存意见并按需返工。

        幂等保护：非 pending 行重复裁决 → `DIFF_ALREADY_RESOLVED`(409)。

        **accepted 的顺序（正确性优先）**：抢行锁（`SELECT ... FOR UPDATE`）→ 锁内复核仍
        pending → 沙箱内推进基线 → 锁内复核 verdict 未变 → 落行/写 `git_baselines` →
        commit。旧顺序（先 git 后抢锁）有一个 TOCTOU：新 run 收口的 `prepare_review` 若
        插在两者之间，它的 `add -A` + supersede 会让 git 里出现一个用**最新索引**（而非
        本轮固化快照）创建的 `[lcode-baseline] round-N` 提交与 tag，随后本轮 accept 拿到
        409 但基线已脏；更糟的是下一轮若没有进一步改动，`git commit` 会 "nothing to
        commit" → 恒 500。现在 supersede 的 UPDATE 被行锁挡住，锁内复核看到「已被取代」
        → 直接 409 且**不做任何 git 写**。

        代价：持锁期间有一次沙箱 git 往返（commit + tag + rev-parse）。该往返已在
        工作线程执行（S1 卸载，评审项 3——不阻塞事件循环），但 **DB 行锁仍跨 await
        持有**（裁决事务边界不变）；P0 单用户、并发面只有「同一会话的裁决 / run 收口」，
        可接受。git 先于 DB 提交，最坏情况是 git 已推进而 DB 提交失败——重试走 tag
        幂等分支自愈。

        **rejected 的注记顺序**：返工是否会被「会话已有活动 run」挡住，在落行与发事件
        **之前**判定并拼进 feedback（事件一经发射不可变，事后追加只有重读详情的人能看到，
        事件回放 / 多标签页看不到）。预检与真正启动之间仍有窗口：预检是一次 SELECT，
        随后是 commit、一次事件 insert、再进 `post_message` 的复查——窗口是两次 DB 往返
        （毫秒级）；窗口内若有别的请求恰好起了 run，则注记不在事件里，`_start_rework`
        只记 ERROR（接受该极端窗口，不为此引入跨请求的 run 锁）。
        """
        key, row = await self._require_row_with_key(review_id)
        if row.verdict != VERDICT_PENDING:
            raise DiffAlreadyResolved(
                "该差异审查已裁决", detail=f"review={review_id} verdict={row.verdict}"
            )

        now = self._now()
        if payload.verdict == "accepted":
            # ① 先抢行锁：supersede / 并发裁决都会被挡住（旧实现是 git 先跑，留有脏基线窗口）
            locked = await self._lock_pending(key, review_id)
            # ② 锁内推进基线（同步沙箱 git；唯一的长外部调用，见 docstring 的代价说明）
            baseline = await self._advance_baseline(locked)
            # ③ 锁内复核：git 往返期间本行不得被任何并发写者改写
            await self._recheck_pending(locked, review_id)
            locked.verdict = "accepted"
            locked.decided_by = DECIDED_BY_USER
            locked.feedback = None
            locked.resolved_at = now
            self.db.add(
                GitBaseline(
                    user_id=locked.user_id,
                    session_id=locked.session_id,
                    round=locked.round,
                    kind="accepted",
                    ref=baseline.tag,
                    commit=baseline.commit,
                    note=f"diff review {locked.id} accepted",
                )
            )
            await self.db.flush()
            await self.db.commit()
            if not baseline.created:
                BusinessLog(
                    message=(
                        "accepted 时索引无变化（或命中幂等分支）：本轮不建空提交，"
                        f"基线沿用当前 HEAD round={locked.round} commit={baseline.commit}"
                    ),
                    action="裁决diff审查",
                    entity="diff_review",
                    entity_id=str(locked.id),
                ).warning()
            await self._safe_emit_resolved(
                locked, feedback=None, decided_by=DECIDED_BY_USER, now=now
            )
            return _to_detail(locked)

        locked = await self._lock_pending(key, review_id)
        feedback = payload.feedback
        # 返工冲突预检：注记必须在 commit + emit 之前进 feedback（事件不可变）
        note = (
            REWORK_NOT_STARTED_NOTE
            if (feedback or "").strip() and await self._has_active_run(locked.session_id)
            else None
        )
        locked.verdict = "rejected"
        locked.decided_by = DECIDED_BY_USER
        locked.feedback = _with_system_note(feedback, note)
        locked.resolved_at = now
        await self.db.commit()
        # 事件里的 feedback 与详情同一字段、同一内容（含可能的 `[系统]` 注记）：
        # 契约不限定它是「纯用户文本」，supersede 已用 system 反馈占用该字段
        await self._safe_emit_resolved(
            locked, feedback=locked.feedback, decided_by=DECIDED_BY_USER, now=now
        )
        await self._keep_worktree_editable(locked)
        if (feedback or "").strip():
            await self._start_rework(locked, feedback or "")
        return _to_detail(locked)

    async def _recheck_pending(self, row: DiffReview, review_id: str) -> None:
        """锁内复核：git 往返之后裁决仍是 pending（行锁在 commit 前一直由本事务持有）。

        行锁已经排除了「走同一把锁的写者」，这里再读一次是防御性复核（例如未来出现
        不走 `_lock_pending` 的写路径）；冲突即 409，且**不做任何 git 写**。
        """
        await self.db.refresh(row)
        if row.verdict != VERDICT_PENDING:
            raise DiffAlreadyResolved(
                "该差异审查已被裁决或取代",
                detail=f"review={review_id} verdict={row.verdict}",
            )

    async def _has_active_run(self, session_id: uuid.UUID) -> bool:
        """返工启动的冲突预检：与 `MessageService.post_message` 用同一谓词。

        返工启动器就是 `MessageService.post_message`（见 api 层装配）；预检若用另一套
        判定就会给出错误结论，因此直接复用 `ACTIVE_RUN_STATUSES`。
        """
        active = await self.db.scalar(
            select(Run.id).where(Run.session_id == session_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        )
        return active is not None

    async def _advance_baseline(self, row: DiffReview) -> BaselineCommit:
        """accepted：沙箱内 commit（已暂存快照）+ tag `lcode-baseline/round-N` → 结果。

        git 往返（commit + tag + rev-parse）经 `asyncio.to_thread` 在工作线程执行
        （S1 卸载，评审项 3）——verdict 是 HTTP 请求路径，不得在事件循环上做秒级
        沙箱调用；DB 行锁仍跨 await 持有（裁决事务边界不变）。
        """
        baseline = await asyncio.to_thread(self._commit_baseline_sync, row)
        if not check_commit_sha(baseline.commit):  # pragma: no cover —— commit_baseline 已校验
            raise GitWorkspaceError("基线提交 sha 非法", detail=f"sha={baseline.commit!r}")
        return baseline

    def _commit_baseline_sync(self, row: DiffReview) -> BaselineCommit:
        """（工作线程内执行）`_live_client`（可能 resume）+ 沙箱内 git commit/tag。"""
        client = self._live_client(str(row.session_id))
        workspace = self._workspace(client)
        return workspace.commit_baseline(row.round, note=f"diff review {row.id}")

    async def _keep_worktree_editable(self, row: DiffReview) -> None:
        """rejected 后撤销 diff 前的暂存（spike §4：`add -A` 之后 `reset`，工作区可编辑）。

        `_live_client`（可能 resume）与 `git reset` 都在工作线程执行（S1 卸载）；两段
        失败隔离的日志口径保持不变（沙箱不可用 ≠ reset 失败）。
        """
        try:
            client = await asyncio.to_thread(self._live_client, str(row.session_id))
        except SandboxError as exc:
            BusinessLog(
                message="驳回后沙箱不可用，跳过 git reset（工作区暂存态保留）",
                action="裁决diff审查",
                entity="diff_review",
                entity_id=str(row.id),
            ).warning(exc)
            return
        workspace = self._workspace(client)
        try:
            await asyncio.to_thread(workspace.unstage_all)
        except SandboxError as exc:
            BusinessLog(
                message="驳回后 git reset 失败（工作区仍可编辑，仅暂存态残留）",
                action="裁决diff审查",
                entity="diff_review",
                entity_id=str(row.id),
            ).warning(exc)

    async def _start_rework(self, row: DiffReview, feedback: str) -> None:
        """驳回返工：经消息通道起新 run（feedback 注入为用户消息）。

        冲突分支（m3-plan 风险 5）：`verdict` 已在落行 / 发事件**之前**预检过活动 run 并把
        系统注记写进 feedback；这里若仍撞 `RUN_ALREADY_ACTIVE`（预检与启动之间的极端窗口：
        另一请求恰好起了 run），**不改行、不改事件**，只记 ERROR——事件已按「无注记」发射，
        回填行只会让 REST 与事件长期不一致（详情说没返工、事件回放说会返工）。
        其它失败同样不得改写「裁决已成立」——只记 ERROR。
        """
        if self._rework_starter is None:
            BusinessLog(
                message="未注入返工启动器（驳回落库，未自动返工）",
                action="启动返工",
                entity="diff_review",
                entity_id=str(row.id),
            ).warning()
            return
        content = build_rework_message(round_=row.round, feedback=feedback)
        try:
            await self._rework_starter(str(row.session_id), content)
        except RunAlreadyActive as exc:
            BusinessLog(
                message=(
                    "预检未发现活动 run，但启动返工时冲突（极端窗口）："
                    "本轮 diff_resolved 不含「返工未自动启动」注记"
                ),
                action="启动返工",
                entity="diff_review",
                entity_id=str(row.id),
            ).error(exc)
        except Exception as exc:  # noqa: BLE001 —— 返工启动失败不得改写裁决结果
            BusinessLog(
                message="驳回返工启动失败（裁决已落库，可手工继续对话）",
                action="启动返工",
                entity="diff_review",
                entity_id=str(row.id),
            ).error(exc)

    async def _safe_emit_resolved(
        self,
        row: DiffReview,
        *,
        feedback: str | None,
        decided_by: str,
        now: datetime,
    ) -> None:
        """回声发射的故障隔离：回显失败不得改写「裁决已成立」（同 approvals 决策口径）。"""
        try:
            await self._emit_resolved(row, feedback=feedback, decided_by=decided_by, now=now)
        except Exception as exc:  # noqa: BLE001 —— 事件出口故障不得让已提交的裁决变 500
            BusinessLog(
                message="diff 裁决回声发射失败（裁决已落库，客户端可经详情重读）",
                action="裁决diff审查",
                entity="diff_review",
                entity_id=str(row.id),
            ).error(exc)

    async def _emit_resolved(
        self,
        row: DiffReview,
        *,
        feedback: str | None,
        decided_by: str,
        now: datetime,
    ) -> None:
        """`diff_resolved` 回声（多标签页一致 + 纯事件回放重建裁决终态）。"""
        await self._pipeline.emit(
            DiffResolvedEvent(
                seq=0,
                session_id=str(row.session_id),
                run_id=str(row.run_id) if row.run_id is not None else None,
                agent=None,
                created_at=now,
                review_id=str(row.id),
                round=row.round,
                verdict=cast(Literal["accepted", "rejected"], row.verdict),
                feedback=feedback,
                decided_by=decided_by,
            )
        )

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _log_patch_skips(session_id: str, file_diffs: list[WorkspaceFileDiff]) -> None:
        """未拉取 patch 的文件记 WARNING（结构化字段在 jsonb 的 `patch_skipped`，不静默）。"""
        skipped = [entry for entry in file_diffs if entry.patch_skipped is not None]
        if not skipped:
            return
        oversize = sum(1 for entry in skipped if entry.patch_skipped == "oversize")
        budget = len(skipped) - oversize
        BusinessLog(
            message=(
                f"{len(skipped)} 个文件的 patch 未拉取（oversize={oversize} budget={budget}），"
                "审查面板只能经 blob_ref 取正文"
            ),
            action="准备diff审查",
            entity="diff_review",
            entity_id=session_id,
        ).warning()

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def _has_provisioned_sandbox(self, session_id: str) -> bool:
        """懒加载判据：会话存在**已 provision** 的沙箱实例（未 provision/无实例 = False）。

        diff 只在发生过工具执行（provision 过）的 run 才有意义——对未 provision 实例
        绝不做任何沙箱操作（git 命令会经懒 provision 触发网络，形同虚设，必须短路）。
        """
        if self._manager is None:
            return False
        client = self._manager.get(session_id)
        return client is not None and client.provisioned

    def _workspace(self, client: LcodeSandbox) -> GitWorkspace:
        return GitWorkspace(client, workspace_dir=self._workspace_dir)

    def _live_client(self, session_id: str) -> LcodeSandbox:
        """取会话沙箱（paused 先 resume）；不在册/未 provision → `GitWorkspaceError`。

        懒加载守卫（P2-3）：未 provision 直接诚实报错、零网络——绝不 provision 一个
        空沙箱去算无意义的 diff（run 收口车道已先行短路，此处兜住 verdict 与直接
        调用方；判据与 `_has_provisioned_sandbox` 同源）。
        """
        if self._manager is None:
            raise GitWorkspaceError("沙箱管理器不可用（未注入），无法计算 diff")
        client = self._manager.get(session_id)
        if client is None:
            raise GitWorkspaceError(
                "会话沙箱不在册（可能已崩溃/释放），无法计算 diff",
                detail=f"session={session_id}",
            )
        if not self._has_provisioned_sandbox(session_id):
            raise GitWorkspaceError(
                "会话沙箱尚未 provision（本会话从未实际执行过工具），无法计算 diff",
                detail=f"session={session_id}",
            )
        if self._manager.stable_phase(session_id) == "paused":
            return self._manager.resume(session_id)
        return client

    async def _require_row(self, review_id: str) -> DiffReview:
        return (await self._require_row_with_key(review_id))[1]

    async def _require_row_with_key(self, review_id: str) -> tuple[uuid.UUID, DiffReview]:
        try:
            key = uuid.UUID(review_id)
        except ValueError as exc:
            raise DiffReviewNotFound(
                f"差异审查不存在：{review_id}",
                detail="路径参数不是合法 UUID",
                original_exception=exc,
            ) from exc
        row = await self.db.get(DiffReview, key)
        if row is None:
            raise DiffReviewNotFound(f"差异审查不存在：{review_id}")
        return key, row

    async def _lock_pending(self, key: uuid.UUID, review_id: str) -> DiffReview:
        """锁内重判 pending（`SELECT ... FOR UPDATE` + 强制刷新，随 commit 释放）。"""
        row = await self.db.get(DiffReview, key, with_for_update=True, populate_existing=True)
        if row is None:
            raise DiffReviewNotFound(f"差异审查不存在：{review_id}")
        if row.verdict != VERDICT_PENDING:
            raise DiffAlreadyResolved(
                "该差异审查已裁决", detail=f"review={review_id} verdict={row.verdict}"
            )
        return row


# --------------------------------------------------------------------------- 短事务入口


async def latest_baseline(session: AsyncSession, session_id: uuid.UUID) -> GitBaseline | None:
    """该会话最近一条基线（round 最大；同轮按 created_at 兜底排序）。"""
    return await session.scalar(
        select(GitBaseline)
        .where(GitBaseline.session_id == session_id)
        .order_by(GitBaseline.round.desc(), GitBaseline.created_at.desc())
        .limit(1)
    )


async def prepare_review_short_tx(
    *,
    session_id: str,
    run_id: str | None,
    user_id: str,
    manager: SandboxManager,
    pipeline: EventPipeline,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    workspace_dir: str | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> DiffReviewDetail | None:
    """run 收口侧的 prepare 入口（自持短事务；由 `RunService` 的 diff_runner 调用）。

    传入的会话只作 service 形态占位（prepare 全程走 `session_factory` 的短事务，git 命令
    在两次短事务之间执行，不占连接）。
    """
    factory = session_factory or get_session_factory()
    async with factory() as session:
        service = DiffService(
            session,
            pipeline=pipeline,
            manager=manager,
            session_factory=factory,
            workspace_dir=workspace_dir,
            now=now,
        )
        return await service.prepare_review(session_id=session_id, run_id=run_id, user_id=user_id)


def _snapshot_of(row: GitBaseline) -> BaselineSnapshot:
    return BaselineSnapshot(
        kind=cast(BaselineKind, row.kind), ref=row.ref, commit=row.commit, round=row.round
    )


def _uuid_of(value: str) -> uuid.UUID:
    """wire id → UUID；非法即 `INVALID_ARGUMENT`（服务内部约定，非契约错误码面）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise InvalidArgument(
            f"id 非法：{value}", detail="不是合法 UUID", original_exception=exc
        ) from exc


__all__ = [
    "DECIDED_BY_SYSTEM",
    "DECIDED_BY_USER",
    "PATCH_LIMIT_BYTES",
    "REWORK_NOT_STARTED_NOTE",
    "SUPERSEDED_FEEDBACK_TEMPLATE",
    "SYSTEM_NOTE_PREFIX",
    "BaselineSnapshot",
    "DiffService",
    "ReworkStarter",
    "build_rework_message",
    "latest_baseline",
    "prepare_review_short_tx",
]
