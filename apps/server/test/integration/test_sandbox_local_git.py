"""integration：round-0 兜底基线（AC-002-5，非 git 输入 → init 提交 + tag）。

对齐 test-plan-m2 §2.2 的 AC-002-5 行：`test_sandbox_local_git.py::test_non_git_input_
creates_lcode_baseline_commit`（本地 git、无外部凭证、**永远跑**）。git 操作真跑本地 git
（`FakeSandbox` 的 execute 在 tmp 工作目录起真子进程，test-plan §3.1），**提交消息与
tag 语义全部以 `contracts/src/lcode_contracts/baseline.py` 为锚**：

- 提交消息 = `BaselineCommitMessage(kind="init", round=0, note=…).render()`
  （`[lcode-baseline] init: <note>`）；
- tag = `tag_for_round(0)`（`lcode-baseline/round-0`），`parse_tag` 回读 kind=init；
- commit sha 过 `check_commit_sha`（40 位小写十六进制）。

验证侧不信任被测代码自己的返回值：用**独立的 git 进程**（`git -C <tmp>`）读工作区的
真实状态做断言（log/tag/ls-files/rev-parse），被测代码与验证通道互不共享实现。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.sandbox.base import SandboxError
from lcode_contracts.baseline import (
    BaselineCommitMessage,
    check_commit_sha,
    parse_tag,
    tag_for_round,
)
from test.support.fake_sandbox import FakeSandbox

# --------------------------------------------------------------------------- 环境守卫


@pytest.fixture(autouse=True)
def _require_local_git() -> Iterator[None]:
    """本模块的判据依赖真实 git（测试计划 §3.1：git 操作真跑本地 git）。"""
    if shutil.which("git") is None:
        pytest.skip("本机无 git：AC-002-5 本地基线用例需要真实 git（缺基础设施显式跳过）")
    yield


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """FakeSandbox 的工作根目录（tmp_path 自动清理）。"""
    root = tmp_path / "workspace"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sandbox(workspace: Path) -> Iterator[FakeSandbox]:
    client = FakeSandbox(root=workspace, sandbox_id="fake-git-0001", provider="bailian")
    client.create()
    yield client


def _git(root: Path, *args: str) -> str:
    """独立验证通道：直接跑 git 读工作区真实状态（不经过被测代码）。"""
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


# --------------------------------------------------------------------------- AC-002-5


def test_non_git_input_creates_lcode_baseline_commit(sandbox: FakeSandbox, workspace: Path) -> None:
    """非 git 输入：init 提交 + `lcode-baseline/round-0` tag，消息/tag 全走契约渲染。"""
    # 现场准备：模拟"本地文件夹载入"——经 upload_files 数据面写入两个文件（硬约束 10）
    uploads = sandbox.upload_files(
        [
            ("src/main.py", b"print('hello')\n"),
            ("README.md", b"# demo\n"),
        ]
    )
    assert [response.error for response in uploads] == [None, None]

    assert sandbox.is_git_repository() is False  # 前置：确实不是 git 仓库

    init = sandbox.create_init_baseline(note="local folder import")

    # 消息与 tag 都以契约为锚（渲染值 + 契约回读）
    expected_message = BaselineCommitMessage(
        kind="init", round=0, note="local folder import"
    ).render()
    assert init.message == expected_message
    assert expected_message == "[lcode-baseline] init: local folder import"

    assert init.tag == tag_for_round(0)
    assert init.tag == "lcode-baseline/round-0"
    parsed = parse_tag(init.tag)
    assert parsed.round == 0
    assert parsed.kind == "init"

    # 独立 git 通道验证真实工作区状态
    sha = _git(workspace, "rev-parse", "HEAD")
    assert sha == init.commit
    assert check_commit_sha(sha)  # 40 位小写十六进制（契约口径）
    assert _git(workspace, "log", "-1", "--pretty=%s") == expected_message
    assert "lcode-baseline/round-0" in _git(workspace, "tag", "--list").splitlines()
    # 数据面写入的文件全部进了基线提交
    tracked = _git(workspace, "ls-files").splitlines()
    assert "src/main.py" in tracked
    assert "README.md" in tracked

    # init 之后工作目录就是 git 仓库（后续轮次 diff 以它为锚）
    assert sandbox.is_git_repository() is True


def test_empty_directory_input_still_creates_baseline(
    sandbox: FakeSandbox, workspace: Path
) -> None:
    """空目录输入（A3 §4.4 分支 3）：--allow-empty 保证基线提交存在、tag 照打。"""
    assert sandbox.is_git_repository() is False

    init = sandbox.create_init_baseline(note="empty workspace")

    expected = BaselineCommitMessage(kind="init", round=0, note="empty workspace").render()
    assert init.message == expected
    assert _git(workspace, "log", "-1", "--pretty=%s") == expected
    assert "lcode-baseline/round-0" in _git(workspace, "tag", "--list").splitlines()
    assert check_commit_sha(init.commit)


def test_init_baseline_refuses_on_existing_git_repo(sandbox: FakeSandbox, workspace: Path) -> None:
    """守卫：已是 git 仓库（git 输入分支）时拒绝 init——绝不改写用户仓库历史。"""
    subprocess.run(
        ["git", "-C", str(workspace), "init", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sandbox.is_git_repository() is True

    with pytest.raises(SandboxError) as excinfo:
        sandbox.create_init_baseline(note="should not happen")

    assert "已是 git 仓库" in str(excinfo.value)
    # 用户仓库保持干净：没有任何基线提交/tag 被写入（空仓库 HEAD 不存在）
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode != 0  # 没有任何提交
    assert _git(workspace, "tag", "--list") == ""


def test_init_baseline_is_reproducible_and_tagged_once(
    sandbox: FakeSandbox, workspace: Path
) -> None:
    """同一现场重复断言一致性：消息可由契约 parse 回读（round-trip）。"""
    init = sandbox.create_init_baseline(note="roundtrip")

    parsed_message = BaselineCommitMessage.parse(init.message)
    assert parsed_message.kind == "init"
    assert parsed_message.round == 0
    assert parsed_message.note == "roundtrip"

    tags = [line for line in _git(workspace, "tag", "--list").splitlines() if line]
    assert tags == ["lcode-baseline/round-0"]  # 恰好一个命名空间 tag，不多打
