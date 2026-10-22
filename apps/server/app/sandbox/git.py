"""沙箱内 git 原语层（M3 Phase 4，REQ-007 / AC-007-1/4；m3-spikes §4 口径）。

**为什么单独一层**：工作区在沙箱里，宿主进程不能直接 `git -C <宿主路径>`。本模块把每条
git 命令经 `SandboxClient.execute`（deepagents 协议面，`LcodeSandbox.execute`）送进沙箱
执行，只解析 `stdout`（`ExecuteResponse.output` 为合并输出，成功路径 stderr 为空）与
`exit_code`；`services/diff_service.py` 只依赖本层的语义方法，不拼任何 shell 字符串。

命令口径（spike S4 逐条验证过）：

- 统一前缀 `git -C <workspace> -c core.quotepath=false <子命令>`——`-c` 必须在子命令前；
  `core.quotepath=false` 保证非 ASCII 路径原样 UTF-8（不出现 ``\\344\\270\\255`` 转义）；
- 纳入新文件：`add -N .`（intent-to-add，空文件 numstat 为 ``0\\t0``）；
- rename 检测要先 `add -A`（裸 mv 在 ``-M`` 下只显示 D；staged 后才配成 R）——保持工作区
  可编辑的做法是 diff 前 `add -A`、驳回后 `reset`（见 `DiffService.verdict`）；
- 总览 ``diff <base> --numstat -M``；状态 ``diff <base> --name-status -M``；
- 逐文件 patch ``diff <base> -M -- <path>``；**rename 必须传双路径**（只传新路径退化为 A、
  只传旧路径退化为 D）；
- 基线推进 ``add -A`` → ``commit -m "[lcode-baseline] round-N"`` → ``tag
  lcode-baseline/round-N`` → ``rev-parse HEAD``（重复 tag 是 fatal+128，天然幂等防护）；
  **索引无变化时不建空提交**（``diff --cached --quiet`` 预检，见 `commit_baseline`）。

解析口径（照抄 spike，不自行发明）：

- numstat 每行 ``added\\tdeleted\\tpath``，binary 为 ``-\\t-``；**只按前两个 tab 切**
  （``split("\\t", 2)``），path 不去引号；
- rename 的 numstat path 两形态 ``dir/{old => new}`` / ``old => new``，按 ``" => "`` 切；
  **rename 判定以 ``--name-status -M`` 为准**（``Rxxx\\told\\tnew`` 三段直切）；
- 相似度不足仍是 D+A（两条独立行），合并逻辑必须容忍；
- 含空格路径的 ``---/+++`` 头行尾多一个 ``\\t``，解析时 strip 尾部单个 tab。

工作区解析（D7）：``SANDBOX_WORKSPACE_DIR`` 可覆盖，否则取沙箱默认 cwd；repo root 顺序 =
根目录 → 唯一一级子目录（兼容 ``git clone <url> repo``）；**多一级子目录且根不是 git 时
明确报错，不猜**。
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from typing import Literal

from deepagents.backends.protocol import ExecuteResponse

from app.core.config import sandbox_settings
from app.sandbox.base import (
    BASELINE_TIMEOUT_SECONDS,
    GIT_IDENTITY_EMAIL,
    GIT_IDENTITY_NAME,
    LcodeSandbox,
    SandboxError,
)
from lcode_contracts.baseline import (
    BaselineCommitMessage,
    check_commit_sha,
    tag_for_round,
)

GIT_TIMEOUT_SECONDS = BASELINE_TIMEOUT_SECONDS
"""单条 git 命令墙钟上限（与 round-0 基线助手同口径：大仓库 clone 后首次 diff 也够用）。"""

LIST_DIRS_COMMAND = "find . -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print"
"""列一级子目录（GNU/BSD find 都支持 `-mindepth/-maxdepth`；隐藏目录排除）。"""

PWD_COMMAND = "pwd"
"""沙箱默认 cwd（D7：未配置 SANDBOX_WORKSPACE_DIR 时的工作区起点）。"""

ChangeKind = Literal["added", "modified", "deleted", "renamed"]
PatchSkipReason = Literal["oversize", "budget"]
"""patch 未拉取的原因：`oversize` = numstat 预估超单文件上限；`budget` = 单轮预算耗尽。"""

PATCH_LIMIT_BYTES: int = 1024 * 1024
"""单文件 patch 上限（1MB）：超出即不落正文（REST 返回 null + blob_ref，m3-plan 风险 4）。"""

ESTIMATED_PATCH_BYTES_PER_LINE: int = 64
"""numstat 预估口径：每个增删行按 64 字节粗估。

**刻意偏保守**：真实的 avg 行长远超 64 的行会被预判跳过（少给 patch、只给 blob_ref），
比反过来「预估通过 → 真把大 patch 读进宿主内存」便宜得多；预估失效（单行超长/二进制
伪装）仍由服务层 `_file_payload` 的读后截断兜底。
"""

MAX_PATCH_FILES: int = 500
"""单轮最多拉取 patch 的文件数。

这是**延迟预算**而非内存预算：每个文件一次沙箱执行往返（数十到数百毫秒），文件数很多
时逐个拉 patch 会把 run 收口拖到分钟级；超出的文件走 blob_ref（审查面板按需取）。
"""

MAX_TOTAL_PATCH_BYTES: int = 4 * 1024 * 1024
"""单轮 patch 累计字节上限（4MB）：超预算的文件同样走 blob_ref（不静默，带 `patch_skipped`）。"""

_MAX_PATH_IN_MESSAGE = 200


class GitWorkspaceError(SandboxError):
    """工作区/git 原语失败（沙箱不可用、命令非零退出、工作区无法解析）。

    继承 `SandboxError`：调用方（diff 服务）按沙箱故障同一口径处理——收口路径降级为
    「无 diff」，请求路径按 500 收敛（不伪装成业务码）。
    """


class GitCommandError(GitWorkspaceError):
    """沙箱内 git 命令非零退出（带命令原文与输出尾部，供日志排查）。"""

    def __init__(self, command: str, exit_code: int | None, output: str) -> None:
        tail = output.strip()
        if len(tail) > 400:
            tail = "..." + tail[-400:]
        super().__init__(
            f"沙箱内 git 命令失败（exit={exit_code}）：{command}",
            detail=tail,
        )
        self.command = command
        self.exit_code = exit_code
        self.output = output


# --------------------------------------------------------------------------- 数据形态


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """工作区解析结果（D7）。

    `root` 恒为绝对路径；`is_repo=False` 表示需要 `ensure_init_baseline()` 兜底（AC-002-5）；
    `head` 为 `None` 表示空仓库（git init 过但没有提交）——这类输入同样走 init 基线，
    否则第 1 轮 diff 没有对照点。
    """

    root: str
    is_repo: bool
    head: str | None


@dataclass(frozen=True, slots=True)
class NameStatusEntry:
    """`--name-status -M` 的一行（rename 判定的事实源）。"""

    change: ChangeKind
    path: str
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class NumstatEntry:
    """`--numstat -M` 的一行；binary 的 additions/deletions 归 0（wire 层非负约束）。"""

    additions: int
    deletions: int
    path: str
    old_path: str | None
    binary: bool
    raw_path: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileDiff:
    """单文件 diff（patch 已按 `---/+++` 头口径归一；binary 的 patch 为 None）。

    `patch is None` 有三种来源，靠另外两个字段区分：

    - `binary=True`：git 判定二进制，无文本 patch；
    - `patch_skipped="oversize" | "budget"`：**没有拉取**（numstat 预估超限 / 预算耗尽），
      `estimated_patch_bytes` 给预估量级；
    - 其余 `patch is None` 只可能是空 patch（git 对某些元数据改动输出空文本）。
    """

    path: str
    old_path: str | None
    change: ChangeKind
    additions: int
    deletions: int
    binary: bool
    patch: str | None
    patch_skipped: PatchSkipReason | None = None
    estimated_patch_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class PatchBudget:
    """本轮 diff 的 patch 拉取预算（`collect_diff` 的三层保护参数）。

    ① `max_patch_bytes` + `estimated_bytes_per_line` = numstat 预判（单文件，不拉 patch）；
    ② `max_total_patch_bytes` / `max_patch_files` = 单轮总量与文件数预算；
    ③ 服务层读后截断（同样的 `max_patch_bytes`，防预估失效）不在本层。
    """

    max_patch_bytes: int = PATCH_LIMIT_BYTES
    max_total_patch_bytes: int = MAX_TOTAL_PATCH_BYTES
    max_patch_files: int = MAX_PATCH_FILES
    estimated_bytes_per_line: int = ESTIMATED_PATCH_BYTES_PER_LINE

    def estimated_patch_bytes(self, *, additions: int, deletions: int) -> int:
        """增删行数 → patch 字节粗估（非负，便于预算与排序）。"""
        return (max(additions, 0) + max(deletions, 0)) * self.estimated_bytes_per_line


@dataclass(frozen=True, slots=True)
class BaselineCommit:
    """一次基线推进的结果（`commit_baseline`）。

    `created=False` 表示**没有创建新提交**：索引与 HEAD 一致（无变化 → 沿用 HEAD 作为
    本轮基线）或命中幂等分支（tag 已存在 / HEAD 已是该轮基线提交）。调用方据此记
    WARNING（避免把「什么都没提交」当成基线推进成功而静默）。
    """

    commit: str
    tag: str
    created: bool


# --------------------------------------------------------------------------- 纯解析


def split_rename_path(path_field: str) -> tuple[str, str] | None:
    """numstat 的 rename path 字段 → (old, new)；非 rename 形态返回 None。

    两种形态：``dir/{old.txt => new.txt}``（花括号内只有文件名，公共前缀/后缀分别拼两侧）
    与 ``old.txt => new.txt``（跨目录，无花括号）。``{a => b}/file.txt`` 这类目录改名也
    按「前缀 + 括号内 + 后缀」还原：old=``a/file.txt``、new=``b/file.txt``。
    路径本身含 ``" => "`` 的极端情况由调用方先按 raw path 精确匹配兜住（见
    `_match_numstat`）。
    """
    if " => " not in path_field:
        return None
    if "{" in path_field and "}" in path_field:
        open_at = path_field.index("{")
        close_at = path_field.index("}", open_at)
        prefix = path_field[:open_at]
        inside = path_field[open_at + 1 : close_at]
        suffix = path_field[close_at + 1 :]
        old_part, sep, new_part = inside.partition(" => ")
        if not sep:
            return None
        return f"{prefix}{old_part}{suffix}", f"{prefix}{new_part}{suffix}"
    old_part, sep, new_part = path_field.partition(" => ")
    if not sep:
        return None
    return old_part, new_part


def parse_numstat(text: str) -> list[NumstatEntry]:
    """解析 `--numstat -M` 输出；容忍合并 stderr 的噪声行（解析失败即跳过）。

    只按前两个 tab 切（``split("\\t", 2)``）：path 段不再二次切分，因此含空格/中文的
    路径原样保留（``core.quotepath=false`` 下 git 也不加引号）。
    """
    entries: list[NumstatEntry] = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path_field = parts
        binary = added_raw == "-" or deleted_raw == "-"
        if binary:
            additions, deletions = 0, 0
        else:
            if not (added_raw.isdigit() and deleted_raw.isdigit()):
                continue  # 噪声行（stderr 合并进 output 时可能出现）
            additions, deletions = int(added_raw), int(deleted_raw)
        if not path_field:
            continue
        renamed = split_rename_path(path_field)
        entries.append(
            NumstatEntry(
                additions=additions,
                deletions=deletions,
                path=renamed[1] if renamed else path_field,
                old_path=renamed[0] if renamed else None,
                binary=binary,
                raw_path=path_field,
            )
        )
    return entries


def parse_name_status(text: str) -> list[NameStatusEntry]:
    """解析 `--name-status -M` 输出（rename 判定的事实源）。

    容忍的码：A/M/D/R/C/T/U（C 按 added、T/U 按 modified 折叠成契约四值）；未知码跳过。
    相似度不足时 git 输出独立的 D 与 A 两行——这里如实保留两条，合并逻辑按路径对位。
    """
    entries: list[NameStatusEntry] = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t")
        code = parts[0].strip()
        if not code:
            continue
        if code[0] == "R" and len(parts) >= 3:
            entries.append(NameStatusEntry(change="renamed", path=parts[2], old_path=parts[1]))
        elif code[0] == "C" and len(parts) >= 3:
            # copy 没有契约语义（DiffFileSummary 只有 renamed 带 old_path）→ 按 added 呈现
            entries.append(NameStatusEntry(change="added", path=parts[2]))
        elif len(parts) >= 2 and parts[1]:
            change: ChangeKind | None = None
            if code[0] == "A":
                change = "added"
            elif code[0] == "D":
                change = "deleted"
            elif code[0] in ("M", "T", "U"):
                change = "modified"
            if change is not None:
                entries.append(NameStatusEntry(change=change, path=parts[1]))
    return entries


def normalize_patch(patch: str) -> str:
    """去掉 ``---``/``+++`` 头行尾多出的单个 tab（含空格路径的 git 输出形态）。"""
    lines = patch.split("\n")
    for index, line in enumerate(lines):
        if (line.startswith("--- ") or line.startswith("+++ ")) and line.endswith("\t"):
            lines[index] = line[:-1]
    return "\n".join(lines)


def _match_numstat(rows: list[NumstatEntry], entry: NameStatusEntry) -> NumstatEntry | None:
    """name-status 行 → numstat 行（按路径对位；rename 优先用 old+new 双键）。

    先按 raw path 精确匹配：路径字面量本身含 ``" => "`` 时不会被 split 误判；再按
    (old_path, path) 双键匹配 rename；最后退化为单 path 匹配。
    """
    for row in rows:
        if row.raw_path == entry.path:
            return row
    if entry.change == "renamed" and entry.old_path is not None:
        for row in rows:
            if row.path == entry.path and row.old_path == entry.old_path:
                return row
    for row in rows:
        if row.path == entry.path:
            return row
    return None


def _file_diff(
    entry: NameStatusEntry,
    stat: NumstatEntry | None,
    *,
    patch: str | None,
    patch_skipped: PatchSkipReason | None = None,
    estimated_patch_bytes: int | None = None,
) -> WorkspaceFileDiff:
    """name-status 行 + numstat 行 → `WorkspaceFileDiff`（行数取 numstat，缺失归 0）。"""
    return WorkspaceFileDiff(
        path=entry.path,
        old_path=entry.old_path,
        change=entry.change,
        additions=stat.additions if stat is not None else 0,
        deletions=stat.deletions if stat is not None else 0,
        binary=bool(stat.binary) if stat is not None else False,
        patch=patch,
        patch_skipped=patch_skipped,
        estimated_patch_bytes=estimated_patch_bytes,
    )


# --------------------------------------------------------------------------- 原语层


class GitWorkspace:
    """把一条条 git 命令送进沙箱执行并解析的薄抽象（diff_service 的唯一依赖面）。

    生命周期：构造 → `resolve_layout()`（一次；结果缓存）→ 语义方法。所有命令都带
    `-C <root>` 与 `-c core.quotepath=false`；参数一律 `shlex.quote`，不在 Python 侧做
    字符串拼接（路径/中文/空格都安全）。
    """

    def __init__(
        self,
        client: LcodeSandbox,
        *,
        workspace_dir: str | None = None,
        timeout: int = GIT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._workspace_dir = (
            workspace_dir if workspace_dir is not None else sandbox_settings.workspace_dir
        ).strip()
        self._timeout = timeout
        self._layout: WorkspaceLayout | None = None

    # ------------------------------------------------------------------ 工作区解析

    @property
    def layout(self) -> WorkspaceLayout:
        """已解析的工作区（未解析时先解析；缓存避免每条命令都探一次）。"""
        if self._layout is None:
            self._layout = self._resolve_layout()
        return self._layout

    @property
    def root(self) -> str:
        """git 仓库根（绝对路径）。"""
        return self.layout.root

    def abs_path(self, relative: str) -> str:
        """仓库内相对路径 → 沙箱内绝对路径（blob_ref 用）。"""
        return posixpath.normpath(posixpath.join(self.root, relative))

    def _resolve_layout(self) -> WorkspaceLayout:
        start = self._workspace_dir or self._default_cwd()
        start = start.rstrip("/") or "/"
        top = self._try_toplevel(start)
        if top is not None:
            return WorkspaceLayout(root=top, is_repo=True, head=self._try_head(top))

        subdirs = self._list_direct_subdirs(start)
        if len(subdirs) == 1:
            candidate = posixpath.join(start, subdirs[0])
            candidate_top = self._try_toplevel(candidate)
            if candidate_top is not None:
                return WorkspaceLayout(
                    root=candidate_top, is_repo=True, head=self._try_head(candidate_top)
                )
            # 唯一子目录不是仓库：回落到根目录按「非 git 输入」处理（AC-002-5）
            return WorkspaceLayout(root=start, is_repo=False, head=None)
        if not subdirs:
            return WorkspaceLayout(root=start, is_repo=False, head=None)
        listed = "、".join(subdirs[:5]) + ("…" if len(subdirs) > 5 else "")
        raise GitWorkspaceError(
            "工作区不是 git 仓库，且包含多个一级子目录，无法确定仓库根（不猜测）",
            detail=f"workspace={start} subdirs=[{listed}]；可用 SANDBOX_WORKSPACE_DIR 指定具体仓库",
        )

    def _default_cwd(self) -> str:
        result = self._client.execute(PWD_COMMAND, timeout=self._timeout)
        cwd = result.output.strip().splitlines()[-1].strip() if result.output.strip() else ""
        if result.exit_code != 0 or not cwd:
            raise GitWorkspaceError(
                "无法获取沙箱默认工作目录（pwd 失败）",
                detail=f"exit={result.exit_code} output={result.output[:200]!r}",
            )
        return cwd

    def _list_direct_subdirs(self, root: str) -> list[str]:
        command = f"cd {shlex.quote(root)} && {LIST_DIRS_COMMAND}"
        result = self._execute(command)
        if result.exit_code != 0:
            raise GitCommandError(command, result.exit_code, result.output)
        names = []
        for line in result.output.splitlines():
            name = line.strip()
            if name.startswith("./"):
                name = name[2:]
            if name:
                names.append(name)
        return sorted(names)

    def _try_toplevel(self, directory: str) -> str | None:
        result = self._execute(self._git_command(directory, ["rev-parse", "--show-toplevel"]))
        if result.exit_code != 0:
            return None
        toplevel = result.output.strip().splitlines()[-1].strip() if result.output.strip() else ""
        return toplevel or None

    def _try_head(self, root: str) -> str | None:
        result = self._execute(self._git_command(root, ["rev-parse", "HEAD"]))
        if result.exit_code != 0:
            return None
        sha = result.output.strip().splitlines()[-1].strip() if result.output.strip() else ""
        return sha if check_commit_sha(sha) else None

    # ------------------------------------------------------------------ 基线

    def ensure_init_baseline(self, *, note: str | None = None) -> tuple[str, str]:
        """round-0 兜底基线（非 git 输入 / 空仓库）：init + add + commit + tag + rev-parse。

        产出与 `LcodeSandbox.create_init_baseline` 完全一致（契约 `BaselineCommitMessage`
        与 `tag_for_round(0)` 为同一渲染源），区别只在命令带 `-C <resolved root>`——当
        `SANDBOX_WORKSPACE_DIR` 指向 cwd 之外的目录时，基线必须落在解析出的工作区。
        tag 已存在时按 idempotent 处理（返回 tag 指向的提交，不重复打 tag）。
        """
        root = self.root
        tag = tag_for_round(0)
        existing = self._try_rev_parse(tag)
        if existing is not None:
            self._layout = WorkspaceLayout(root=root, is_repo=True, head=existing)
            return existing, tag

        if not self.layout.is_repo:
            self._require_ok(["init", "-q"])
        message = BaselineCommitMessage(kind="init", round=0, note=note).render()
        self._require_ok(["add", "-A"])
        self._require_ok(
            [
                "-c",
                f"user.name={GIT_IDENTITY_NAME}",
                "-c",
                f"user.email={GIT_IDENTITY_EMAIL}",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                message,
            ]
        )
        self._require_ok(["tag", tag])
        commit = self._require_sha(root, ["rev-parse", "HEAD"])
        # 解析结果缓存同步：init 之后工作区就是 git 仓库（后续 diff/commit 以此为锚）
        self._layout = WorkspaceLayout(root=root, is_repo=True, head=commit)
        return commit, tag

    def commit_baseline(self, round_: int, *, note: str | None = None) -> BaselineCommit:
        """accepted 裁决的基线推进：commit（已暂存索引）→ tag → rev-parse HEAD。

        只提交**索引**（`add -A` 在 `collect_diff` 时已完成）：裁决时刻工作区若又被改动，
        未暂存的改动不会混进基线（审查内容 = 固化快照，D6）。重试友好：

        - tag 已存在 → 直接返回该 tag 指向的提交（幂等，`created=False`）；
        - HEAD 已是指定 `[lcode-baseline] round-N` 提交但 tag 缺失（上次在两次命令间失败）
          → 补打 tag 并返回 HEAD（`created=False`）；
        - **索引与 HEAD 一致**（本轮改动在裁决前被撤回/已提交）→ 不建空提交，把当前 HEAD
          记作本轮基线并补 tag（`created=False`，调用方记 WARNING）。裸 `commit` 在这种
          情形下会以非零退出（nothing to commit），旧实现会把 accepted 变成 500 并让该行
          永远 pending——这是本条分支存在的原因；
        - 其余 → 新建 `[lcode-baseline] round-N` 提交 + tag（`created=True`）。
        """
        tag = tag_for_round(round_)
        existing = self._try_rev_parse(tag)
        if existing is not None:
            return BaselineCommit(commit=existing, tag=tag, created=False)

        message = BaselineCommitMessage(kind="accepted", round=round_, note=note).render()
        head_message = self._try_output(["log", "-1", "--pretty=%B"])
        if head_message is not None and head_message.strip() == message:
            self._require_ok(["tag", tag])
            head = self._require_sha(self.root, ["rev-parse", "HEAD"])
            return BaselineCommit(commit=head, tag=tag, created=False)

        if not self.has_staged_changes():
            # 无变化：不造空提交（也不污染用户历史）；把当前 HEAD 当本轮基线并补 tag，
            # 保证 ref/commit 自洽（下一轮 diff 以该 commit 为对照点）
            head = self._require_sha(self.root, ["rev-parse", "HEAD"])
            self._require_ok(["tag", tag])
            return BaselineCommit(commit=head, tag=tag, created=False)

        self._require_ok(
            [
                "-c",
                f"user.name={GIT_IDENTITY_NAME}",
                "-c",
                f"user.email={GIT_IDENTITY_EMAIL}",
                "commit",
                "-q",
                "-m",
                message,
            ]
        )
        self._require_ok(["tag", tag])
        head = self._require_sha(self.root, ["rev-parse", "HEAD"])
        return BaselineCommit(commit=head, tag=tag, created=True)

    def has_staged_changes(self) -> bool:
        """索引相对 HEAD 是否有变化（`diff --cached --quiet`：退出 0=无变化 / 1=有变化）。

        其它退出码（索引损坏、无 HEAD 等）按 git 命令失败处理——不猜。
        """
        args = ["diff", "--cached", "--quiet"]
        result = self._run(args)
        if result.exit_code == 0:
            return False
        if result.exit_code == 1:
            return True
        raise GitCommandError(self._git_command(self.root, args), result.exit_code, result.output)

    # ------------------------------------------------------------------ 索引

    def stage_intent_to_add(self) -> None:
        """`add -N .`：只登记新文件的存在（intent-to-add），不暂存内容。"""
        self._require_ok(["add", "-N", "."])

    def stage_all(self) -> None:
        """`add -A`：暂存全部改动（rename 检测与 accepted 提交的前置条件）。"""
        self._require_ok(["add", "-A"])

    def unstage_all(self) -> None:
        """`reset`：撤销暂存、保留工作区（驳回后让工作区继续可编辑，spike §4）。"""
        self._require_ok(["reset"])

    # ------------------------------------------------------------------ diff

    def numstat(self, base_commit: str) -> list[NumstatEntry]:
        return parse_numstat(self._require_ok(["diff", base_commit, "--numstat", "-M"]))

    def name_status(self, base_commit: str) -> list[NameStatusEntry]:
        return parse_name_status(self._require_ok(["diff", base_commit, "--name-status", "-M"]))

    def file_patch(self, base_commit: str, *, path: str, old_path: str | None = None) -> str:
        """单文件 patch；rename 必须传双路径（只传一侧会退化成 A 或 D）。"""
        paths = [old_path, path] if old_path is not None and old_path != path else [path]
        return normalize_patch(self._require_ok(["diff", base_commit, "-M", "--", *paths]))

    def collect_diff(
        self, base_commit: str, *, budget: PatchBudget | None = None
    ) -> list[WorkspaceFileDiff]:
        """暂存快照 → 全量文件 diff（空列表 = 工作区无变化，D6 不发事件）。

        顺序：`add -A` → `--name-status -M`（rename 事实源）→ `--numstat -M`（行数）→
        逐文件 patch（rename 双路径）。无变化时 `reset` 撤销暂存，工作区保持干净可编辑。

        **patch 拉取的三层保护**（`budget` 可覆盖默认预算；大 patch 不进宿主内存）：

        ① numstat 预判：增删行 × `estimated_bytes_per_line` 超 `max_patch_bytes` 的文件
           **不执行 `diff`**，直接按 `patch_skipped="oversize"` 产出（e2b 的 `execute`
           只能整段返回，读过就已经驻留内存——预判是唯一避开的办法）；
        ② 单轮预算：候选按预估字节升序（小文件优先，确定性排序）逐个拉取，累计**实际**
           字节或文件数超预算的文件标 `patch_skipped="budget"`；
        ③ 读后截断不在本层（服务层 `_file_payload` 按实际字节兜底，防预估失效）。
        """
        limit = budget or PatchBudget()
        if self.layout.is_repo is False:
            raise GitWorkspaceError(
                "工作区不是 git 仓库，无法计算 diff（先 ensure_init_baseline）",
                detail=f"workspace={self.layout.root}",
            )
        self.stage_all()
        status_entries = self.name_status(base_commit)
        if not status_entries:
            self.unstage_all()
            return []
        stats = self.numstat(base_commit)

        diffs: dict[str, WorkspaceFileDiff] = {}
        candidates: list[tuple[int, NameStatusEntry, NumstatEntry | None]] = []
        for entry in status_entries:
            stat = _match_numstat(stats, entry)
            if stat is not None and stat.binary:
                diffs[entry.path] = _file_diff(entry, stat, patch=None)
                continue
            estimate = (
                limit.estimated_patch_bytes(additions=stat.additions, deletions=stat.deletions)
                if stat is not None
                else 0
            )
            if estimate > limit.max_patch_bytes:
                diffs[entry.path] = _file_diff(
                    entry,
                    stat,
                    patch=None,
                    patch_skipped="oversize",
                    estimated_patch_bytes=estimate,
                )
                continue
            candidates.append((estimate, entry, stat))

        total_patch_bytes = 0
        fetched = 0
        for estimate, entry, stat in sorted(candidates, key=lambda item: (item[0], item[1].path)):
            over_files = fetched >= limit.max_patch_files
            over_bytes = total_patch_bytes + estimate > limit.max_total_patch_bytes
            if over_files or over_bytes:
                diffs[entry.path] = _file_diff(
                    entry, stat, patch=None, patch_skipped="budget", estimated_patch_bytes=estimate
                )
                continue
            patch = self.file_patch(base_commit, path=entry.path, old_path=entry.old_path)
            # 按实际字节计账（预估只用于预判与排序）：单行超长的文件在这里仍可能偏大，
            # 但计入总量后能保证后续文件不再继续堆叠内存
            total_patch_bytes += len(patch.encode("utf-8"))
            fetched += 1
            diffs[entry.path] = _file_diff(entry, stat, patch=patch)

        return sorted(diffs.values(), key=lambda item: item.path)

    # ------------------------------------------------------------------ 内部

    def _git_command(self, root: str, args: list[str]) -> str:
        """`git -C <root> -c core.quotepath=false <args>`（-c 必须在子命令前）。"""
        parts = ["git", "-C", root, "-c", "core.quotepath=false", *args]
        return " ".join(shlex.quote(part) for part in parts)

    def _execute(self, command: str) -> ExecuteResponse:
        try:
            return self._client.execute(command, timeout=self._timeout)
        except SandboxError as exc:
            raise GitWorkspaceError(f"沙箱内命令执行失败：{exc.message}", detail=command) from exc

    def _run(self, args: list[str]) -> ExecuteResponse:
        return self._execute(self._git_command(self.root, args))

    def _require_ok(self, args: list[str]) -> str:
        result = self._run(args)
        if result.exit_code != 0:
            raise GitCommandError(
                self._git_command(self.root, args), result.exit_code, result.output
            )
        return result.output

    def _try_output(self, args: list[str]) -> str | None:
        result = self._run(args)
        if result.exit_code != 0:
            return None
        return result.output

    def _try_rev_parse(self, ref: str) -> str | None:
        output = self._try_output(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"])
        if output is None:
            return None
        sha = output.strip().splitlines()[-1].strip() if output.strip() else ""
        return sha if check_commit_sha(sha) else None

    def _require_sha(self, root: str, args: list[str]) -> str:
        result = self._execute(self._git_command(root, args))
        if result.exit_code != 0:
            raise GitCommandError(self._git_command(root, args), result.exit_code, result.output)
        sha = result.output.strip().splitlines()[-1].strip() if result.output.strip() else ""
        if not check_commit_sha(sha):
            raise GitWorkspaceError(
                "git 输出中无法解析 40 位提交 sha",
                detail=f"args={args} output={result.output[:200]!r}",
            )
        return sha


def describe_path(path: str) -> str:
    """日志/消息里的路径截断（超长路径不把日志撑爆）。"""
    return path if len(path) <= _MAX_PATH_IN_MESSAGE else path[: _MAX_PATH_IN_MESSAGE - 1] + "…"


__all__ = [
    "ESTIMATED_PATCH_BYTES_PER_LINE",
    "GIT_TIMEOUT_SECONDS",
    "MAX_PATCH_FILES",
    "MAX_TOTAL_PATCH_BYTES",
    "PATCH_LIMIT_BYTES",
    "BaselineCommit",
    "GitCommandError",
    "GitWorkspace",
    "GitWorkspaceError",
    "NameStatusEntry",
    "NumstatEntry",
    "PatchBudget",
    "PatchSkipReason",
    "WorkspaceFileDiff",
    "WorkspaceLayout",
    "describe_path",
    "normalize_patch",
    "parse_name_status",
    "parse_numstat",
    "split_rename_path",
]
