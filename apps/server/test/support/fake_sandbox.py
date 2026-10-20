"""FakeSandbox：本地 tmp 目录 + 真子进程的沙箱替身（test-plan-m2 §3.1）。

三层共用同一实现（unit / integration / e2e），与真适配器的差别只有一条——**执行发生在
本地 tmp 目录而不是远端沙箱**；协议面（`execute` / `upload_files` / `download_files`
/ `id`）与 LCode 扩展面（`create` / `pause` / `resume` / `kill` / `status`）完全一致：

- `execute` 默认在工作根目录下真起子进程，因此 git 操作、文件工具派生命令都是真链路
  （`test_sandbox_local_git.py` 的 AC-002-5 依赖这一点）；
- `upload_files` / `download_files` 走**真 tmp 文件**（deepagents 的文件工具经
  `execute` 派生，写路径经 `upload_files`——两者都真落盘，测试可事后查文件系统）；
- `create` / `pause` / `resume` / `kill` 记调用轨迹（`calls`）与自身相位，供 manager
  的相位机/空闲 pause/重建断言；可按需让某次调用失败（`fail_pause` 等）；
- 剧本注入点 `exec_script: Sequence[ExecStep]`（输出块、退出码、延时、抛
  `SandboxUnavailable`）——AC-002-1/2/3/4 的失败路径都靠剧本驱动，不 mock 方法签名
  之外的内部状态；有剧本时按剧本出结果，无剧本时真跑本地子进程。

**执行形态说明**：`execute` 收到的就是沙箱要执行的 shell 命令原文（与真适配器一致：
e2b 内部同样是 `/bin/bash -l -c <cmd>`，见 `e2b/sandbox_sync/commands/command.py::_start`）。
替身用**参数列表**形式拉起 bash（`["bash", "-lc", command]`，`shell=False`），既保留
shell 语义，又不在 Python 侧做字符串拼接——本替身是有意为之的"代码执行边界"，与
`docs/arch/backend-design.md` §4 的沙箱定位一致。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import sleep

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
)

from app.sandbox.base import (
    LcodeSandbox,
    SandboxFileError,
    SandboxProvider,
    SandboxStatus,
    SandboxUnavailable,
    combine_output,
)


@dataclass
class ExecStep:
    """剧本一步：输出块 / 退出码 / 延时 / 抛错，可选按命令子串匹配。

    `match` 为 None 表示「下一条命令即消费本步」；给了子串则只有包含该子串的命令才
    消费它（其余命令继续按顺序找下一个可匹配的步）。`raises` 优先于输出——沙箱不可用
    的语义就是"什么都不吐"，故抛错步不产生任何输出块。
    """

    stdout_chunks: tuple[str, ...] = ()
    stderr_chunks: tuple[str, ...] = ()
    exit_code: int = 0
    delay_seconds: float = 0.0
    raises: BaseException | None = None
    match: str | None = None

    @classmethod
    def output(cls, *chunks: str, exit_code: int = 0) -> ExecStep:
        """仅输出若干 stdout 块（流式断点论证用：一条命令多块）。"""
        return cls(stdout_chunks=tuple(chunks), exit_code=exit_code)

    @classmethod
    def failure(cls, *, stderr: str = "boom", exit_code: int = 1) -> ExecStep:
        """命令失败（非零退出码 + stderr），沙箱本身仍可用。"""
        return cls(stderr_chunks=(stderr,), exit_code=exit_code)

    @classmethod
    def unavailable(cls, reason: str = "scripted unavailable") -> ExecStep:
        """意外不可用剧本：抛 `SandboxUnavailable`（AC-002-4 重建路径的输入）。"""
        return cls(raises=SandboxUnavailable(reason))


@dataclass
class FakeSandboxState:
    """FakeSandbox 的可观测内部状态（测试断言用，避免断言私有字段）。"""

    created: bool = False
    paused: bool = False
    killed: bool = False
    unavailable: bool = False


class FakeSandbox(LcodeSandbox):
    """`LcodeSandbox` 的本地替身：真 tmp 目录 + 真子进程 + 可注入剧本。"""

    # 默认取百炼——多数 unit 用例不关心 provider，显式给值便于断言事件里的 provider 字段
    provider: SandboxProvider = "bailian"

    def __init__(
        self,
        *,
        root: Path,
        exec_script: Sequence[ExecStep] | None = None,
        provider: SandboxProvider = "bailian",
        sandbox_id: str = "fake-sbx-0001",
        session_id: str | None = None,
        fail_create: bool = False,
        fail_pause: bool = False,
        fail_kill: bool = False,
        real_exec_timeout_seconds: float | None = 60.0,
    ) -> None:
        super().__init__(session_id=session_id)
        self.provider = provider
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.exec_script: list[ExecStep] = list(exec_script or [])
        self.state = FakeSandboxState()
        self.calls: list[str] = []
        """方法调用轨迹（如 ["create", "pause", "kill"]），顺序即发生顺序。"""
        self.commands: list[str] = []
        """execute 收到的命令原文轨迹。"""
        self.uploaded: list[str] = []
        """upload_files 写过的沙箱侧路径轨迹。"""
        self.unavailable_reason = ""
        self.create_timeout_seconds: int | None = None
        self._configured_id = sandbox_id
        self._fail_create = fail_create
        self._fail_pause = fail_pause
        self._fail_kill = fail_kill
        self._real_exec_timeout_seconds = real_exec_timeout_seconds

    # ------------------------------------------------------------------ 生命周期

    def create(self, *, timeout_seconds: int | None = None) -> None:
        self.calls.append("create")
        self.create_timeout_seconds = timeout_seconds
        if self._fail_create:
            self.state.unavailable = True
            raise SandboxUnavailable("剧本设定：创建沙箱失败")
        self.state.created = True
        self._set_sandbox_id(self._configured_id)

    def pause(self) -> None:
        self.calls.append("pause")
        if self._fail_pause:
            self.state.unavailable = True
            raise SandboxUnavailable("剧本设定：pause 失败")
        self.state.paused = True

    def resume(self) -> None:
        self.calls.append("resume")
        if not self.state.created or self.state.killed:
            raise SandboxUnavailable("剧本设定：沙箱不存在，无法恢复")
        self.state.paused = False
        self.state.unavailable = False

    def kill(self) -> None:
        self.calls.append("kill")
        if self._fail_kill:
            raise SandboxUnavailable("剧本设定：kill 失败")
        self.state.killed = True
        self.state.paused = False

    def status(self) -> SandboxStatus:
        self.calls.append("status")
        if self.state.killed:
            return SandboxStatus(
                sandbox_id=self.id, phase="killed", available=False, detail="已终止"
            )
        if not self.state.created or self.state.unavailable:
            return SandboxStatus(
                sandbox_id=self.id,
                phase="crashed",
                available=False,
                detail=self.unavailable_reason or "剧本设定：不可用",
            )
        return SandboxStatus(
            sandbox_id=self.id,
            phase="paused" if self.state.paused else "running",
            available=True,
        )

    def mark_unavailable(self, reason: str = "剧本设定：外部标记不可用") -> None:
        """测试辅助：把替身标成不可用（不必经 execute 剧本就能驱动 probe 路径）。"""
        self.state.unavailable = True
        self.unavailable_reason = reason

    def set_kill_failure(self, enabled: bool) -> None:
        """测试辅助：切换 kill 失败开关（首次失败 → 重试成功的释放序列用）。"""
        self._fail_kill = enabled

    # -------------------------------------------------------------- 协议原语（薄翻译）

    def _execute_command(
        self, command: str, *, timeout: int | None, exec_id: str | None
    ) -> ExecuteResponse:
        self.commands.append(command)
        step = self._take_step(command)
        if step is not None:
            if step.raises is not None:
                self.state.unavailable = True
                raise step.raises
            if step.delay_seconds:
                sleep(step.delay_seconds)
            for chunk in step.stdout_chunks:
                self.emit_output_text("stdout", chunk, exec_id)
            for chunk in step.stderr_chunks:
                self.emit_output_text("stderr", chunk, exec_id)
            return ExecuteResponse(
                output=combine_output("".join(step.stdout_chunks), "".join(step.stderr_chunks)),
                exit_code=step.exit_code,
                truncated=False,
            )

        # 无剧本 → 真跑本地子进程（git / 文件工具派生命令的真链路）。
        # 参数列表形式（shell=False）+ 显式 bash -lc：语义等同沙箱内的命令执行，
        # 但不在 Python 侧拼接 shell 字符串（见模块 docstring）。
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self._real_exec_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.state.unavailable = True
            raise SandboxUnavailable("本地替身执行超时") from exc
        self.emit_output_text("stdout", proc.stdout, exec_id)
        self.emit_output_text("stderr", proc.stderr, exec_id)
        return ExecuteResponse(
            output=combine_output(proc.stdout, proc.stderr),
            exit_code=proc.returncode,
            truncated=False,
        )

    def _write_file(self, path: str, content: bytes) -> None:
        target = self._local(path)
        self.uploaded.append(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except NotADirectoryError as exc:
            raise SandboxFileError("invalid_path", path, detail=str(exc)) from exc
        except PermissionError as exc:
            raise SandboxFileError(PERMISSION_DENIED, path, detail=str(exc)) from exc

    def _read_file(self, path: str) -> bytes:
        target = self._local(path)
        if target.is_dir():
            raise SandboxFileError(IS_DIRECTORY, path, detail="目标是目录")
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise SandboxFileError(FILE_NOT_FOUND, path, detail=str(exc)) from exc
        except PermissionError as exc:
            raise SandboxFileError(PERMISSION_DENIED, path, detail=str(exc)) from exc

    # ---------------------------------------------------------------------- 内部

    def _take_step(self, command: str) -> ExecStep | None:
        for index, step in enumerate(self.exec_script):
            if step.match is None or step.match in command:
                return self.exec_script.pop(index)
        return None

    def _local(self, path: str) -> Path:
        """沙箱侧路径 → 本地 tmp 路径（绝对路径去掉根前缀，保持目录语义一致）。"""
        return self.root / path.lstrip("/")


# ----------------------------------------------------------------- 剧本/夹具小工具


def clone_with_files(root: Path, files: dict[str, str]) -> Path:
    """测试辅助：在 root 下写出一组文件（非 git 输入的"本地文件夹"分支）。"""
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def git_probe_script(*, is_repo: bool) -> list[ExecStep]:
    """git 探针剧本：让 `is_git_repository()` 得到期望答案而真跑 git。"""
    answer = ("true\n",) if is_repo else ("false\n",)
    return [ExecStep(stdout_chunks=answer, match="rev-parse")]


# 供 e2e 夹具在进程内注册"当前假沙箱"的钩子类型（e2e 任务落地时消费）
FakeSandboxRegistry = Callable[[str, FakeSandbox], None]

__all__ = [
    "ExecStep",
    "FakeSandbox",
    "FakeSandboxRegistry",
    "FakeSandboxState",
    "clone_with_files",
    "git_probe_script",
]
