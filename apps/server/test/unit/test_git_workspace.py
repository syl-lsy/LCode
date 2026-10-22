"""unit 层：沙箱 git 原语层（M3 Phase 4，spike §4 命令与解析口径；零库零网络）。

覆盖两块：

1. **纯解析**（`parse_numstat` / `parse_name_status` / `split_rename_path` /
   `normalize_patch`）——直接喂 spike 里实测过的 git 输出原文，锁住「只按前两个 tab 切」
   「rename 以 name-status 为准」「相似度不足退化为 D+A」「头行尾部单 tab」等口径；
2. **命令拼装与工作区解析**（`GitWorkspace`）——用记录命令的桩沙箱断言每条命令都带
   `-C <root> -c core.quotepath=false`（`-c` 在子命令前）、含空格路径经 `shlex.quote`、
   rename patch 传双路径、无变化时 `reset`、多一级子目录且根不是 git 时明确报错。

真 git 链路由 `test/integration/test_diff_review_flow.py` 覆盖（FakeSandbox + 临时真仓库）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from deepagents.backends.protocol import ExecuteResponse

from app.sandbox.base import LcodeSandbox, SandboxUnavailable
from app.sandbox.git import (
    GitCommandError,
    GitWorkspace,
    GitWorkspaceError,
    PatchBudget,
    WorkspaceLayout,
    normalize_patch,
    parse_name_status,
    parse_numstat,
    split_rename_path,
)

SHA = "a" * 40
BASE = "b" * 40


# --------------------------------------------------------------------------- 桩沙箱


class RecordingSandbox:
    """记录命令并按 handler 回放的桩（不实现 LcodeSandbox 抽象面，只 duck-type `execute`）。"""

    def __init__(self, handler: Callable[[str], ExecuteResponse]) -> None:
        self._handler = handler
        self.commands: list[str] = []

    def execute(
        self, command: str, *, timeout: int | None = None, exec_id: str | None = None
    ) -> ExecuteResponse:
        self.commands.append(command)
        return self._handler(command)


def _workspace(
    handler: Callable[[str], ExecuteResponse], *, workspace_dir: str | None = "/ws"
) -> tuple[GitWorkspace, RecordingSandbox]:
    sandbox = RecordingSandbox(handler)
    return (
        GitWorkspace(cast(LcodeSandbox, cast(Any, sandbox)), workspace_dir=workspace_dir),
        sandbox,
    )


def _reply(output: str = "", exit_code: int = 0) -> ExecuteResponse:
    return ExecuteResponse(output=output, exit_code=exit_code)


# --------------------------------------------------------------------------- 纯解析


def test_parse_numstat_keeps_unicode_space_paths_and_binary() -> None:
    """只按前两个 tab 切：空格/中文路径原样保留；binary 记 `-\\t-` → 0/0。"""
    text = "1\t0\ta b.txt\n3\t2\t中文.txt\n-\t-\tdata.bin\n0\t0\tempty_new.txt\n"
    rows = parse_numstat(text)

    assert [(row.additions, row.deletions, row.path, row.binary) for row in rows] == [
        (1, 0, "a b.txt", False),
        (3, 2, "中文.txt", False),
        (0, 0, "data.bin", True),
        (0, 0, "empty_new.txt", False),
    ]


def test_parse_numstat_rename_two_forms() -> None:
    """同目录 `dir/{old => new}` 与跨目录 `old => new` 两种形态都还原出双路径。"""
    text = "0\t0\tdir/{old.txt => new.txt}\n5\t1\tsrc/top.txt => docs/moved.txt\n"
    rows = parse_numstat(text)

    assert (rows[0].path, rows[0].old_path) == ("dir/new.txt", "dir/old.txt")
    assert (rows[1].path, rows[1].old_path) == ("docs/moved.txt", "src/top.txt")


def test_parse_numstat_ignores_merged_stderr_noise() -> None:
    """`ExecuteResponse.output` 是合并流：stderr 噪声行（非 numstat 形态）必须被跳过。"""
    text = "warning: something odd\n1\t0\tkept.txt\nnot a numstat line\n"
    rows = parse_numstat(text)

    assert [row.path for row in rows] == ["kept.txt"]


def test_parse_name_status_handles_all_codes_and_low_similarity() -> None:
    """A/M/D/R/C/T 全覆盖；相似度不足时 git 输出独立 D+A，两行都如实保留。"""
    text = (
        "D\ta b.txt\n"
        "A\tnewfile.txt\n"
        "A\tsrc/new.txt\n"
        "D\tsrc/old.txt\n"
        "M\t中文.txt\n"
        "R100\tdir/old.txt\tdir/new.txt\n"
        "C75\tbase.txt\tcopy.txt\n"
        "T\tlink.sh\n"
    )
    entries = parse_name_status(text)

    assert [(entry.change, entry.path, entry.old_path) for entry in entries] == [
        ("deleted", "a b.txt", None),
        ("added", "newfile.txt", None),
        ("added", "src/new.txt", None),
        ("deleted", "src/old.txt", None),
        ("modified", "中文.txt", None),
        ("renamed", "dir/new.txt", "dir/old.txt"),
        ("added", "copy.txt", None),
        ("modified", "link.sh", None),
    ]


def test_parse_name_status_skips_unknown_and_malformed_lines() -> None:
    """未知码与缺路径的行跳过（不猜语义），换行/空白行也容忍。"""
    text = "X\twhatever.txt\nA\n\nM\tkept.txt\n"
    assert [entry.path for entry in parse_name_status(text)] == ["kept.txt"]


def test_split_rename_path_forms() -> None:
    """花括号内是文件名（前缀/后缀拼接）；跨目录无花括号；非 rename 返回 None。"""
    assert split_rename_path("dir/{old.txt => new.txt}") == ("dir/old.txt", "dir/new.txt")
    assert split_rename_path("{a => b}/file.txt") == ("a/file.txt", "b/file.txt")
    assert split_rename_path("dir/{a => b}/file.txt") == (
        "dir/a/file.txt",
        "dir/b/file.txt",
    )
    assert split_rename_path("src/top.txt => docs/moved.txt") == (
        "src/top.txt",
        "docs/moved.txt",
    )
    assert split_rename_path("plain.txt") is None


def test_normalize_patch_strips_single_trailing_tab_only_on_headers() -> None:
    """含空格路径的 `---/+++` 头行尾多一个 tab：只剥头行那一个，内容行不动。"""
    patch = (
        "diff --git a/a b.txt b/a b.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/a b.txt\t\n"
        "@@ -0,0 +1 @@\n"
        "+keep\ttab\n"
    )
    normalized = normalize_patch(patch)

    assert "+++ b/a b.txt\n" in normalized
    assert "+keep\ttab" in normalized
    assert "+++ b/a b.txt\t" not in normalized


# --------------------------------------------------------------------------- 工作区解析


def test_layout_uses_sandbox_cwd_and_repo_toplevel() -> None:
    """未配置 SANDBOX_WORKSPACE_DIR：pwd 取 cwd → `rev-parse --show-toplevel` 定 root。"""

    def handler(command: str) -> ExecuteResponse:
        if command == "pwd":
            return _reply("/home/user\n")
        if "--show-toplevel" in command:
            return _reply("/home/user/repo\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler, workspace_dir=None)

    assert workspace.layout == WorkspaceLayout(root="/home/user/repo", is_repo=True, head=SHA)
    assert sandbox.commands[0] == "pwd"
    assert "--show-toplevel" in sandbox.commands[1]


def test_layout_resolves_unique_subdirectory_repo() -> None:
    """根不是 git 且只有唯一一级子目录（`git clone <url> repo`）→ 用该子目录为 root。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command and "/ws/repo" in command:
            return _reply("/ws/repo\n")
        if "--show-toplevel" in command:
            return _reply("", 128)
        if "-maxdepth" in command:
            return _reply("./repo\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    assert workspace.layout == WorkspaceLayout(root="/ws/repo", is_repo=True, head=SHA)


def test_layout_rejects_multiple_subdirs_when_root_is_not_repo() -> None:
    """多一级子目录且根不是 git：明确报错，不猜仓库根（D7）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("", 128)
        if "-maxdepth" in command:
            return _reply("./a\n./b\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitWorkspaceError) as excinfo:
        _ = workspace.layout
    assert "多个一级子目录" in str(excinfo.value)
    assert "a、b" in excinfo.value.detail


def test_layout_empty_workspace_is_non_git() -> None:
    """空目录（非 git 输入）：is_repo=False，等待 ensure_init_baseline 兜底。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("", 128)
        if "-maxdepth" in command:
            return _reply("")
        raise AssertionError(command)

    assert _workspace(handler)[0].layout == WorkspaceLayout(root="/ws", is_repo=False, head=None)


def test_layout_pwd_failure_is_explicit() -> None:
    """pwd 失败 → 明确错误（不把空路径当工作区）。"""

    workspace, _ = _workspace(lambda command: _reply("", 1), workspace_dir=None)

    with pytest.raises(GitWorkspaceError) as excinfo:
        _ = workspace.layout
    assert "默认工作目录" in str(excinfo.value)


def test_layout_subdir_listing_failure_raises_command_error() -> None:
    """列目录命令失败 → `GitCommandError`（带命令与退出码）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("", 128)
        return _reply("boom", 2)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitCommandError) as excinfo:
        _ = workspace.layout
    assert excinfo.value.exit_code == 2


# --------------------------------------------------------------------------- 命令拼装


def _collect_handler(extra: dict[str, str]) -> Callable[[str], ExecuteResponse]:
    """collect_diff 的脚本：add -A / name-status / numstat / diff -- path。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if " add -A" in command:
            return _reply("")
        if "--name-status" in command:
            return _reply(extra["name-status"])
        if "--numstat" in command:
            return _reply(extra["numstat"])
        if " diff " in command:
            return _reply(extra.get("patch", "diff --git a/x b/x\n"))
        raise AssertionError(command)

    return handler


def test_collect_diff_builds_expected_commands_and_passes_rename_double_paths() -> None:
    """命令前缀 / `-c` 位置 / 路径引号 / rename 双路径；结果按新路径排序。"""
    handler = _collect_handler(
        {
            "name-status": "R100\tdir/old.txt\tdir/new.txt\nA\tspace name.md\n",
            "numstat": "0\t0\tdir/{old.txt => new.txt}\n1\t0\tspace name.md\n",
        }
    )
    workspace, sandbox = _workspace(handler)

    diffs = workspace.collect_diff(BASE)

    assert [(diff.path, diff.old_path, diff.change) for diff in diffs] == [
        ("dir/new.txt", "dir/old.txt", "renamed"),
        ("space name.md", None, "added"),
    ]
    diff_commands = [
        command for command in sandbox.commands if " diff " in command and "-M --" in command
    ]
    assert len(diff_commands) == 2
    assert diff_commands[0].endswith("-M -- dir/old.txt dir/new.txt")
    assert "'space name.md'" in diff_commands[1]
    for command in sandbox.commands:
        assert command.startswith("git -C /ws -c core.quotepath=false ")


def test_collect_diff_without_changes_unstages_and_returns_empty() -> None:
    """工作区无变化：不发事件不建行（调用方据空列表收敛），并 `reset` 撤销暂存。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if " add -A" in command:
            return _reply("")
        if "--name-status" in command:
            return _reply("")
        if " reset" in command:
            return _reply("")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler)

    assert workspace.collect_diff(BASE) == []
    assert any(" reset" in command for command in sandbox.commands)


def test_collect_diff_requires_git_repository() -> None:
    """非 git 工作区直接 collect → 明确错误（调用方应先 ensure_init_baseline）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("", 128)
        if "-maxdepth" in command:
            return _reply("")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitWorkspaceError) as excinfo:
        workspace.collect_diff(BASE)
    assert "不是 git 仓库" in str(excinfo.value)


def test_stage_intent_to_add_uses_add_dash_n() -> None:
    """`add -N .` 原语（新文件纳入 diff 的 spike 口径）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        return _reply("")

    workspace, sandbox = _workspace(handler)
    workspace.stage_intent_to_add()

    assert sandbox.commands[-1] == "git -C /ws -c core.quotepath=false add -N ."


def test_sandbox_failure_is_wrapped_as_git_workspace_error() -> None:
    """沙箱抛 `SandboxUnavailable` → 归一到 `GitWorkspaceError`（上层按无 diff/500 收敛）。"""

    def handler(command: str) -> ExecuteResponse:
        raise SandboxUnavailable("远端收回了实例")

    workspace, _ = _workspace(handler)

    with pytest.raises(GitWorkspaceError) as excinfo:
        workspace.stage_all()
    assert "执行失败" in str(excinfo.value)


# --------------------------------------------------------------------------- 基线


def test_ensure_init_baseline_reuses_existing_tag() -> None:
    """tag round-0 已存在（重入/DB 行丢失）→ 幂等返回 tag 指向的提交，不重复 init。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if "rev-parse --verify" in command:
            return _reply(f"{BASE}\n")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler)

    assert workspace.ensure_init_baseline() == (BASE, "lcode-baseline/round-0")
    assert not any(" init " in command for command in sandbox.commands)
    assert not any(" commit " in command for command in sandbox.commands)


def test_commit_baseline_is_idempotent_when_tag_exists() -> None:
    """重复 accepted（tag 已存在）→ 直接返回既有提交（天然幂等防护）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if "rev-parse --verify" in command:
            return _reply(f"{BASE}\n")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler)

    result = workspace.commit_baseline(1)
    assert (result.commit, result.tag, result.created) == (BASE, "lcode-baseline/round-1", False)
    assert not any(" commit " in command for command in sandbox.commands)


def test_commit_baseline_recovers_when_head_already_baseline() -> None:
    """tag 缺失但 HEAD 已是指定基线提交（上次在 commit 与 tag 之间失败）→ 补打 tag。"""
    message = "[lcode-baseline] round-2"

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if "rev-parse --verify" in command:
            return _reply("", 128)
        if "log -1 --pretty=%B" in command:
            return _reply(f"{message}\n")
        if " tag " in command:
            return _reply("")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler)

    result = workspace.commit_baseline(2)
    assert (result.commit, result.tag, result.created) == (SHA, "lcode-baseline/round-2", False)
    assert any(" tag lcode-baseline/round-2" in command for command in sandbox.commands)
    assert not any(" commit " in command for command in sandbox.commands)


def test_commit_baseline_rejects_unparsable_sha() -> None:
    """commit 成功但输出不是 40 位 sha → 明确错误（不把脏值写进 git_baselines）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse --verify" in command:
            return _reply("", 128)
        if "log -1 --pretty=%B" in command:
            return _reply("something else\n")
        if "diff --cached --quiet" in command:
            return _reply("", 1)  # 有暂存改动 → 走创建提交流程
        if " commit " in command:
            return _reply("")
        if " tag " in command:
            return _reply("")
        if "rev-parse HEAD" in command:
            return _reply("not-a-sha\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitWorkspaceError) as excinfo:
        workspace.commit_baseline(1)
    assert "40 位" in str(excinfo.value)


def test_commit_baseline_without_staged_changes_reuses_head() -> None:
    """索引与 HEAD 一致（改动在裁决前被撤回）→ 不建空提交，当前 HEAD 记作本轮基线。

    裸 `git commit` 在这里会 "nothing to commit" 非零退出（旧实现把 accepted 变 500、
    该行永远 pending）；本分支把 HEAD 当基线并补 tag（ref/commit 自洽）。
    """

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse --verify" in command:
            return _reply("", 128)
        if "log -1 --pretty=%B" in command:
            return _reply("previous commit\n")
        if "diff --cached --quiet" in command:
            return _reply("", 0)  # 无变化
        if " tag " in command:
            return _reply("")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, sandbox = _workspace(handler)

    result = workspace.commit_baseline(3)

    assert (result.commit, result.tag, result.created) == (SHA, "lcode-baseline/round-3", False)
    assert any(" tag lcode-baseline/round-3" in command for command in sandbox.commands)
    assert not any(" commit " in command for command in sandbox.commands)


def test_commit_baseline_creates_commit_when_index_changed() -> None:
    """索引有变化 → 建 `[lcode-baseline] round-N` 提交 + tag，`created=True`。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse --verify" in command:
            return _reply("", 128)
        if "log -1 --pretty=%B" in command:
            return _reply("previous commit\n")
        if "diff --cached --quiet" in command:
            return _reply("", 1)
        if " commit " in command or " tag " in command:
            return _reply("")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    result = workspace.commit_baseline(1)

    assert (result.commit, result.tag, result.created) == (SHA, "lcode-baseline/round-1", True)


def test_has_staged_changes_maps_exit_codes() -> None:
    """`diff --cached --quiet`：0=无变化 / 1=有变化 / 其它=命令失败（不猜）。"""

    def handler(exit_code: int) -> Callable[[str], ExecuteResponse]:
        def _handler(command: str) -> ExecuteResponse:
            if "--show-toplevel" in command:
                return _reply("/ws\n")
            if "rev-parse HEAD" in command:
                return _reply(f"{SHA}\n")
            if "diff --cached --quiet" in command:
                return _reply("fatal: index file corrupt" if exit_code > 1 else "", exit_code)
            raise AssertionError(command)

        return _handler

    assert _workspace(handler(0))[0].has_staged_changes() is False
    assert _workspace(handler(1))[0].has_staged_changes() is True
    with pytest.raises(GitCommandError) as excinfo:
        _workspace(handler(128))[0].has_staged_changes()
    assert excinfo.value.exit_code == 128


def test_parse_numstat_skips_malformed_rows_and_empty_path() -> None:
    """非数字计数 / 缺 path / 非 binary 的 `-` 行全部跳过（噪声容忍）。"""
    text = "a\tb\tc\n1\t0\t\n1\t0\n"
    assert parse_numstat(text) == []
    # 单侧 `-` 视为 binary（git 正常会两侧都给 `-`，但容忍脏输出）
    assert [(row.path, row.binary) for row in parse_numstat("-\t5\tx.txt\n")] == [("x.txt", True)]


def test_split_rename_path_returns_none_for_braces_without_arrow() -> None:
    """花括号里没有 ` => `（不是 rename 形态）→ None。"""
    assert split_rename_path("dir/{single}.txt") is None


def test_abs_path_joins_repo_root() -> None:
    """blob_ref 用绝对路径：仓库相对路径 → 沙箱内绝对路径（normpath 去 `..`）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    assert workspace.abs_path("src/deep.py") == "/ws/src/deep.py"
    assert workspace.abs_path("src/../a.txt") == "/ws/a.txt"


def test_layout_single_non_repo_subdir_falls_back_to_root() -> None:
    """唯一子目录不是仓库 → 回落到根目录按「非 git 输入」处理（AC-002-5）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("", 128)
        if "-maxdepth" in command:
            return _reply("./folder\n")
        raise AssertionError(command)

    assert _workspace(handler)[0].layout == WorkspaceLayout(root="/ws", is_repo=False, head=None)


def test_layout_ignores_empty_toplevel_output() -> None:
    """`rev-parse --show-toplevel` 退出 0 但输出空 → 视为未解析（不把空串当 root）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("\n")
        if "-maxdepth" in command:
            return _reply("")
        raise AssertionError(command)

    assert _workspace(handler)[0].layout == WorkspaceLayout(root="/ws", is_repo=False, head=None)


def test_collect_diff_matches_rename_by_plain_new_path() -> None:
    """numstat 的 rename 行若是裸新路径形态（无花括号/箭头），按单 path 匹配仍对位。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if " add -A" in command:
            return _reply("")
        if "--name-status" in command:
            return _reply("R100\told.txt\tnew.txt\n")
        if "--numstat" in command:
            return _reply("0\t0\tnew.txt\n")
        if " diff " in command:
            return _reply("diff --git a/old.txt b/new.txt\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    diffs = workspace.collect_diff(BASE)

    assert [(diff.path, diff.old_path) for diff in diffs] == [("new.txt", "old.txt")]


# --------------------------------------------------------------------------- patch 预算


def _budget_handler(
    name_status: str, numstat: str, *, patch_bytes: int = 40
) -> Callable[[str], ExecuteResponse]:
    """collect_diff 预算脚本：add -A / name-status / numstat / 逐文件 patch（定长正文）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if " add -A" in command:
            return _reply("")
        if "--name-status" in command:
            return _reply(name_status)
        if "--numstat" in command:
            return _reply(numstat)
        if "-M -- " in command:
            return _reply("x" * patch_bytes)
        raise AssertionError(command)

    return handler


def test_collect_diff_skips_oversize_patch_by_numstat_estimate() -> None:
    """numstat 预估超单文件上限 → 该文件**不执行** `diff`（预判替代读后截断）。"""
    # 20000 行 × 64B/行 = 1.28MB > 1MB；small.txt 远低于阈值
    handler = _budget_handler(
        "A\tbig.txt\nA\tsmall.txt\n",
        "20000\t0\tbig.txt\n2\t1\tsmall.txt\n",
    )
    workspace, sandbox = _workspace(handler)

    diffs = workspace.collect_diff(BASE)
    by_path = {diff.path: diff for diff in diffs}

    assert by_path["big.txt"].patch is None
    assert by_path["big.txt"].patch_skipped == "oversize"
    assert by_path["big.txt"].estimated_patch_bytes == 20000 * 64
    assert by_path["small.txt"].patch == "x" * 40
    assert by_path["small.txt"].patch_skipped is None
    assert not any("-M -- big.txt" in command for command in sandbox.commands)


def test_collect_diff_skips_files_over_total_byte_budget() -> None:
    """单轮累计字节预算（实际字节计账）耗尽后的文件 → `patch_skipped="budget"`，不拉 patch。"""
    paths = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    handler = _budget_handler(
        "".join(f"A\t{path}\n" for path in paths),
        "".join(f"1\t0\t{path}\n" for path in paths),
    )
    workspace, sandbox = _workspace(handler)

    diffs = workspace.collect_diff(BASE, budget=PatchBudget(max_total_patch_bytes=200))
    by_path = {diff.path: diff for diff in diffs}

    # 每文件预估 64B、实际 40B：a→40 / b→80 / c→120 / d→160 都进预算，e 超（160+64 > 200）
    assert [path for path in paths if by_path[path].patch is not None] == paths[:4]
    assert by_path["e.txt"].patch_skipped == "budget"
    assert by_path["e.txt"].estimated_patch_bytes == 64
    assert not any("-M -- e.txt" in command for command in sandbox.commands)


def test_collect_diff_prefers_small_patches_under_file_budget() -> None:
    """文件数预算下小文件优先（排序键 = (预估字节, 路径)），大文件走 blob_ref。"""
    handler = _budget_handler(
        "A\tbig.txt\nA\ttiny.txt\n",
        "100\t0\tbig.txt\n1\t0\ttiny.txt\n",
    )
    workspace, _ = _workspace(handler)

    diffs = workspace.collect_diff(BASE, budget=PatchBudget(max_patch_files=1))
    by_path = {diff.path: diff for diff in diffs}

    assert by_path["tiny.txt"].patch == "x" * 40
    assert by_path["big.txt"].patch is None
    assert by_path["big.txt"].patch_skipped == "budget"


def test_collect_diff_budget_does_not_touch_binary_files() -> None:
    """binary 文件照旧 `patch=None`（不计入预算、不带 patch_skipped）。"""
    handler = _budget_handler("A\tbin.dat\n", "-\t-\tbin.dat\n")
    workspace, sandbox = _workspace(handler)

    diffs = workspace.collect_diff(BASE)

    assert [(diff.path, diff.binary, diff.patch, diff.patch_skipped) for diff in diffs] == [
        ("bin.dat", True, None, None)
    ]
    assert not any("-M -- bin.dat" in command for command in sandbox.commands)


def test_require_ok_raises_git_command_error_with_output() -> None:
    """`add -A` 非零退出 → `GitCommandError`（命令原文 + 退出码 + 输出尾部）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        if " add -A" in command:
            return _reply("fatal: unable to write index", 128)
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitCommandError) as excinfo:
        workspace.stage_all()
    assert excinfo.value.exit_code == 128
    assert "unable to write index" in excinfo.value.output


def test_require_sha_raises_on_nonzero_rev_parse() -> None:
    """基线提交后 `rev-parse HEAD` 失败 → `GitCommandError`（不返回空 sha）。"""

    def handler(command: str) -> ExecuteResponse:
        if "--show-toplevel" in command:
            return _reply("/ws\n")
        if "rev-parse --verify" in command:
            return _reply("", 128)
        if "rev-parse HEAD" in command:
            return _reply("fatal: bad revision", 128)
        if (
            " init " in command
            or " add -A" in command
            or " commit " in command
            or " tag " in command
        ):
            return _reply("")
        raise AssertionError(command)

    workspace, _ = _workspace(handler)

    with pytest.raises(GitCommandError) as excinfo:
        workspace.ensure_init_baseline()
    assert excinfo.value.exit_code == 128


def test_describe_path_truncates_long_paths() -> None:
    """日志辅助：超长路径截断并加省略号（不把日志撑爆）。"""
    from app.sandbox.git import describe_path

    short = "src/a.py"
    long_path = "a" * 300

    assert describe_path(short) == short
    assert len(describe_path(long_path)) == 200
    assert describe_path(long_path).endswith("…")


def test_git_command_error_truncates_long_output() -> None:
    """错误详情只保留输出尾部 400 字符（长 stderr 不整段进日志/异常）。"""
    output = "x" * 1000
    error = GitCommandError("git -C /ws diff", 1, output)

    assert error.detail.startswith("...")
    assert len(error.detail) == 403


def test_workspace_dir_override_skips_pwd() -> None:
    """`SANDBOX_WORKSPACE_DIR` 覆盖：直接从指定目录解析 repo root（不跑 pwd）。"""

    def handler(command: str) -> ExecuteResponse:
        if command == "pwd":
            raise AssertionError("配置了覆盖目录时不应取沙箱 cwd")
        if "--show-toplevel" in command and "/custom/repo" in command:
            return _reply("/custom/repo\n")
        if "rev-parse HEAD" in command:
            return _reply(f"{SHA}\n")
        raise AssertionError(command)

    workspace, _ = _workspace(handler, workspace_dir="/custom/repo")

    assert workspace.layout == WorkspaceLayout(root="/custom/repo", is_repo=True, head=SHA)
