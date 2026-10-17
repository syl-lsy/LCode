#!/usr/bin/env python3
"""按 git 变更跑关联测试（LCode 阶段环口径，docs/arch/test-plan-m2.md §4）。

行为约定：
- 默认变更集 = staged（已暂存）+ unstaged（工作区）+ untracked（未跟踪）三路并集；
  ``--since REF`` 在此基础上并上「自该 ref 起的已提交变更」（``git diff REF...HEAD``，
  按 merge-base 比较）——对应 ``make test-changed SINCE=<ref>``；
- Python 侧：测试目录清单读根 pyproject.toml 的 ``[tool.test-runner] test_dirs``
  （现口径 ``apps/*/test`` + ``contracts/tests``），变更文件落在测试目录内、且是测试
  文件（test_*.py / *_test.py，conftest/__init__ 除外）才逐个 ``uv run pytest <file> -q``
  串行执行；已被删除的变更文件跳过。**源码改动不自动映射测试**（§4.1：改了哪个测试
  跑哪个，受影响集的人工选择依据见 §4.2 期望表）；
- Web 侧（最小实现，**整跑口径**）：变更集中存在 ``web/`` 文件时执行一次
  ``pnpm --dir web test``（vitest 全量）。选全量而非 ``vitest related`` 的理由：
  related 在无匹配用例时非零退出、且骨架期前端全量仅 22 用例/数秒——全量成本等价、
  行为确定（M2 组件测试变多后如需 related 再评估）；
- 非 git 仓库直接清晰报错并 exit 1（本脚本依赖 git 定位变更集，不做猜测）；
- 无匹配变更时打印提示并 exit 0；任一失败最终 exit 1。

Makefile 入口：``make test-changed [SINCE=<ref>]``。定位是开发期快速回归，
替代不了 ``make test`` 的门禁（unit + integration + 覆盖率 80%）；CI 走
``make ci-backend`` / ``make ci-frontend`` 全量，不调本脚本。
"""

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# git 命令失败（非 git 仓库、unborn HEAD 等）由调用方按场景处理
GIT_REPO_CHECK_ARGS = ["rev-parse", "--is-inside-work-tree"]

# web 变更判定：这些前缀下的文件不算（依赖/产物/覆盖率输出）
WEB_IGNORED_PREFIXES = ("web/node_modules/", "web/dist/", "web/coverage/")


def _git(repo_root: Path, args: list[str]) -> list[str] | None:
    """执行 git 命令并返回非空输出行；命令失败返回 None（由调用方决定是否忽略）。

    注意不 raise：unborn HEAD（仓库尚无首个提交）会让 diff 类命令失败，
    这属于正常开发早期状态，不应中断脚本。
    `-c core.quotepath=false`：中文等非 ASCII 文件名原样输出——git 默认会把它们
    转义成带引号的八进制（`"test_\\344\\270\\255..."`），路径匹配会静默失配漏跑测试。
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def get_repo_root() -> Path:
    """定位仓库根；非 git 仓库打印清晰提示并退出。"""
    result = subprocess.run(["git", *GIT_REPO_CHECK_ARGS], capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip() != "true":
        print("错误: 当前目录不在 git 仓库内，无法定位变更文件。", file=sys.stderr)
        print(
            "提示: 先 `git init`（并提交一次）后重试；本命令只按 git 变更跑关联测试。",
            file=sys.stderr,
        )
        sys.exit(1)
    top_level = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(top_level.stdout.strip())


def get_changed_files(repo_root: Path, since: str | None) -> set[Path]:
    """变更集：staged + unstaged + untracked 三路并集；--since 再并上 ref 以来的提交。"""
    changed: set[Path] = set()

    has_head = _git(repo_root, ["rev-parse", "--verify", "--quiet", "HEAD"]) is not None
    commands: list[list[str]] = []
    if has_head:
        commands.append(["diff", "--name-only", "--cached", "HEAD"])  # 已暂存
        commands.append(["diff", "--name-only", "HEAD"])  # 工作区未暂存
    else:
        # unborn HEAD：还没有可比的提交，暂存区视为全部「新增」（intersect 后仍只跑测试文件）
        commands.append(["ls-files", "--cached"])
    commands.append(["ls-files", "--others", "--exclude-standard"])  # 未跟踪

    if since is not None:
        if _git(repo_root, ["rev-parse", "--verify", "--quiet", f"{since}^{{commit}}"]) is None:
            print(f"错误: --since 引用的 ref 不存在：{since}", file=sys.stderr)
            print("提示: 先 `git tag -l` / `git branch -a` 确认 ref 名。", file=sys.stderr)
            sys.exit(1)
        # 三点 = 从 merge-base 比较：不含 ref 一侧独有的提交
        commands.append(["diff", "--name-only", f"{since}...HEAD"])

    for cmd in commands:
        for line in _git(repo_root, cmd) or []:
            changed.add(repo_root / line)

    return changed


def load_test_dirs(repo_root: Path) -> list[Path]:
    """读根 pyproject 的 [tool.test-runner] test_dirs 并展开为已存在的目录。"""
    config_path = repo_root / "pyproject.toml"
    if not config_path.exists():
        print(
            "错误: 根目录缺少 pyproject.toml，无法读取 [tool.test-runner] test_dirs。",
            file=sys.stderr,
        )
        return []

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    patterns: list[str] = config.get("tool", {}).get("test-runner", {}).get("test_dirs", [])
    if not patterns:
        print("错误: pyproject.toml 的 [tool.test-runner] 中未配置 test_dirs。", file=sys.stderr)
        return []

    dirs: list[Path] = []
    for pattern in patterns:
        for p in sorted(repo_root.glob(pattern)):
            if p.is_dir():
                dirs.append(p)

    return dirs


def _is_test_file(file: Path) -> bool:
    """只有测试文件才值得单文件直跑；conftest/__init__ 由 pytest 收集时自动加载。"""
    if file.suffix != ".py":
        return False
    name = file.stem
    if name in ("__init__", "conftest"):
        return False
    return name.startswith("test_") or name.endswith("_test")


def find_test_files(changed_files: set[Path], test_dirs: list[Path]) -> list[Path]:
    """变更文件 ∩ 测试目录 ∩ 测试文件命名 ∩ 仍存在，按路径排序保证串行顺序稳定。"""
    test_files: list[Path] = []
    for f in sorted(changed_files):
        if not _is_test_file(f) or not f.is_file():
            continue
        for test_dir in test_dirs:
            try:
                f.relative_to(test_dir)
                test_files.append(f)
                break
            except ValueError:
                continue
    return test_files


def has_web_changes(changed_files: set[Path], repo_root: Path) -> bool:
    """变更集中是否存在 web/ 源文件（排除依赖/产物目录）。"""
    for f in changed_files:
        try:
            rel = f.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if (rel == "web" or rel.startswith("web/")) and not rel.startswith(WEB_IGNORED_PREFIXES):
            return True
    return False


def run_test(test_file: Path, repo_root: Path) -> bool:
    rel_path = test_file.relative_to(repo_root)
    # flush=True：本脚本的 stdout 被管道接走时是块缓冲，不刷新会把头部信息排到子进程输出之后
    print(f"\n{'=' * 60}", flush=True)
    print(f"运行: {rel_path}", flush=True)
    print(f"{'=' * 60}", flush=True)

    result = subprocess.run(
        ["uv", "run", "pytest", str(test_file), "-q"],
        cwd=repo_root,
    )
    return result.returncode == 0


def run_web_tests(repo_root: Path) -> bool:
    """web 侧整跑：一次 `pnpm --dir web test`（vitest 全量，口径见模块 docstring）。"""
    print(f"\n{'=' * 60}", flush=True)
    print("运行: web/ 变更 → pnpm --dir web test（vitest 全量，骨架期不挂覆盖率阈值）", flush=True)
    print(f"{'=' * 60}", flush=True)

    if shutil.which("pnpm") is None:
        print(
            "错误: 变更含 web/ 文件但 pnpm 不可用；安装 pnpm 或直接跑 make test-web。",
            file=sys.stderr,
        )
        return False

    return subprocess.run(["pnpm", "--dir", "web", "test"], cwd=repo_root).returncode == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 git 变更跑关联测试（阶段环，非门禁）")
    parser.add_argument(
        "--since",
        metavar="REF",
        default=None,
        help="追加「自该 ref 起的已提交变更」（git diff REF...HEAD）并入变更集",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = get_repo_root()
    changed_files = get_changed_files(repo_root, args.since)

    if not changed_files:
        print("没有变更的文件，无需跑关联测试。")
        return

    test_dirs = load_test_dirs(repo_root)
    if not test_dirs:
        sys.exit(1)

    test_files = find_test_files(changed_files, test_dirs)
    web_needed = has_web_changes(changed_files, repo_root)

    if not test_files and not web_needed:
        print("变更集中无测试文件（test_*.py / *_test.py）也无 web/ 文件，无需跑关联测试。")
        return

    if test_files:
        print(f"发现 {len(test_files)} 个变更的测试文件:", flush=True)
        for f in test_files:
            print(f"  {f.relative_to(repo_root)}")
    if web_needed:
        print("变更集含 web/ 文件 → 追加一次 vitest 全量", flush=True)

    failed: list[str] = []
    passed = 0
    for test_file in test_files:
        if run_test(test_file, repo_root):
            passed += 1
        else:
            failed.append(str(test_file.relative_to(repo_root)))

    if web_needed:
        if run_web_tests(repo_root):
            passed += 1
        else:
            failed.append("web/（vitest 全量）")

    print(f"\n{'=' * 60}")
    print(f"结果: {passed} 通过, {len(failed)} 失败")
    if failed:
        print("失败的测试:")
        for name in failed:
            print(f"  {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
