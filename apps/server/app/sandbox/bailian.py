"""百炼沙箱适配器（E2B 协议；`e2b==2.31.0` 钉死——AGENTS.md 硬约束 1）。

对齐面（A3 §4.2）：LCode 只翻译三个原语到 e2b SDK，其余由 `LcodeSandbox` 承担：

- `_execute_command` → `Sandbox.commands.run(cmd, timeout=…, on_stdout=…, on_stderr=…)`；
  输出回调桥成 `sandbox_output` 事件源（AC-002-1）；**非零退出**（e2b 抛
  `CommandExitException`，继承 `CommandResult` 带 stdout/stderr/exit_code）是命令结果
  而非沙箱故障，映射回 `ExecuteResponse`（round-0 的 `is_git_repository()` 依赖它）；
- `_write_file` → `sandbox.files.write(path, data)`（`POST /files?path=…` 数据面；
  硬约束 10：批量载入走数据面，不靠模版挂载）；
- `_read_file` → `sandbox.files.read(path, format="bytes")`（返回 `bytearray` → `bytes`）；
- `create` → `Sandbox.create(api_url=…, api_key="e2b_<uid>", headers={"Authorization": …},
  template=…, timeout=…)`；真实密钥经 `Authorization` 头传参，**不进环境变量以外的
  配置/日志**（NFR-004）；
- `pause`/`resume`/`kill` → `handle.pause()/connect()/kill()`——百炼口径：
  **只有显式 pause 才保留状态**（硬约束 8，模板空闲自动休眠不保状态）；
- `status` → `handle.get_info().state`（paused/running）；只读探测，
  失败即 `available=False`（不抛错）。

三条实测细节（官方接入口径 + 读安装版源码得出，写进代码注释以免后人踩）：

1. **必须传 `validate_api_key=False`**：e2b 客户端对 `api_key` 做 `e2b_<hex>` 前缀校验
   （`.venv/.../e2b/api/__init__.py::validate_api_key`）。百炼官方口径是 `api_key` 仅作
   格式占位（`e2b_<aliyun_uid>` 或任意符合格式的占位符），**服务端不用它做业务鉴权**；
   真实鉴权走 `headers={"Authorization": "Bearer <BAILIAN_API_KEY>"}`。关掉客户端预校验
   后百炼密钥不必满足 e2b 格式。注：`headers` 在 e2b 2.31 已 deprecated（等价
   `api_headers`），保留 `headers` 系官方百炼文档口径；升级 e2b 时随官方文档同步。
2. **完整 URL 传 `api_url`，不要传 `domain`**：e2b 会把 `domain` 拼成
   `https://api.<domain>`（旧实现即因此 DNS 失败）。`domain` 是创建响应返回的运行时
   访问域名，不是配置项。
3. **resume 走 `handle.connect()`**：e2b 2.31.0 没有 `resume()` 方法，
   `connect()` 的语义是"连接（paused 则自动恢复）并刷新 timeout"
   （`sandbox_sync/main.py::connect` 文档串）。跨进程恢复（服务重启后用 sandbox_id
   重连）不在 M2 范围，见 manager 的关停口径。

测试缝隙：`sandbox_factory` 可注入假 SDK 工厂（unit 层不连真沙箱）；真冒烟走
`integration/test_sandbox_real.py` 的 `requires_sandbox` 标记。SDK 句柄以 `Any` 承载
（e2b 的生成式类型面不并入本仓库 mypy 门禁，适配器保持薄翻译）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
)
from e2b import (
    AuthenticationException,
    FileNotFoundException,
    InvalidArgumentException,
    Sandbox,
    SandboxNotFoundException,
    TimeoutException,
)
from e2b.sandbox.commands.command_handle import CommandExitException

from app.core.config import SandboxSettings, safe_env_var_name, sandbox_settings
from app.sandbox.base import (
    LcodeSandbox,
    SandboxConfigurationError,
    SandboxError,
    SandboxFileError,
    SandboxLifecycleError,
    SandboxStatus,
    SandboxUnavailable,
    combine_output,
)

DEFAULT_SANDBOX_TIMEOUT_SECONDS = 3600
"""默认生命周期（秒）：1 小时——百炼 Hobby 档上限 3600s，Pro 档可到 24h。

空闲成本靠显式 `pause()` 控制（硬约束 8），不依赖这里的 timeout。
"""


class BailianSandbox(LcodeSandbox):
    """百炼沙箱（E2B 协议兼容）：`provider="bailian"`。"""

    provider = "bailian"

    def __init__(
        self,
        *,
        session_id: str | None = None,
        settings: SandboxSettings | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
        default_timeout_seconds: int = DEFAULT_SANDBOX_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(session_id=session_id)
        cfg = settings or sandbox_settings
        self._api_key_env = cfg.bailian_api_key_env
        self._template = cfg.bailian_template
        self._api_url = cfg.bailian_api_url
        self._aliyun_uid = cfg.bailian_aliyun_uid
        self._default_timeout_seconds = default_timeout_seconds
        # `e2b.Sandbox.create` 的可注入替身（unit 层不连真沙箱）
        self._sandbox_factory: Callable[..., Any] = sandbox_factory or Sandbox.create
        self._handle: Any = None

    # ------------------------------------------------------------------ 凭证（NFR-004）

    def _require_api_key(self) -> str:
        """从 `*_API_KEY_ENV` 指向的环境变量读密钥；缺失时消息**只含变量名**。"""
        env_name = safe_env_var_name(self._api_key_env)
        value = os.environ.get(self._api_key_env, "").strip()
        if not value:
            raise SandboxConfigurationError(
                f"缺少百炼沙箱凭证：环境变量 {env_name} 未设置或为空"
                "（密钥只从环境变量读取，见 NFR-004；不在配置文件/日志中落地）"
            )
        return value

    def _create_kwargs(self, *, timeout_seconds: int | None) -> dict[str, Any]:
        if not self._api_url:
            # 静默省略会让 e2b 落到默认公网端点，最终报「鉴权被拒」——把排障引向错误方向
            raise SandboxConfigurationError(
                "SANDBOX_BAILIAN_API_URL 未配置（百炼 E2B 接入完整地址；"
                "旧配置键 SANDBOX_BAILIAN_DOMAIN 已废弃）"
            )
        api_key = self._require_api_key()
        kwargs: dict[str, Any] = {
            "template": self._template or None,  # 空串 → e2b 默认模板（spike 定模板后填值）
            "timeout": timeout_seconds or self._default_timeout_seconds,
            # 官方口径：api_key 仅作格式占位，服务端不做业务鉴权（见模块 docstring 细节 1）
            "api_key": f"e2b_{self._aliyun_uid}" if self._aliyun_uid else "e2b_0",
            "headers": {"Authorization": f"Bearer {api_key}"},
            # 占位 api_key 不必满足 e2b_ 前缀校验：关闭客户端预校验
            "validate_api_key": False,
            # 完整 URL 交给 api_url；绝不传 domain（会被拼成 https://api.<domain>）
            "api_url": self._api_url,
        }
        return kwargs

    # ---------------------------------------------------------------------- 生命周期

    def create(self, *, timeout_seconds: int | None = None) -> None:
        if self._handle is not None:
            raise SandboxLifecycleError("百炼沙箱已 provision（create 只能调用一次）")
        try:
            handle = self._sandbox_factory(**self._create_kwargs(timeout_seconds=timeout_seconds))
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 —— SDK 异常族统一翻译（见 _translate）
            raise _translate("创建百炼沙箱", exc, api_key_env=self._api_key_env) from exc
        self._handle = handle
        self._set_sandbox_id(str(handle.sandbox_id))

    def pause(self) -> None:
        handle = self._require_handle()
        try:
            handle.pause()
        except Exception as exc:  # noqa: BLE001
            raise _translate("暂停百炼沙箱", exc, api_key_env=self._api_key_env) from exc

    def resume(self) -> None:
        handle = self._require_handle()
        try:
            handle.connect()  # e2b 语义：paused 则自动恢复并刷新 timeout
        except Exception as exc:  # noqa: BLE001
            raise _translate("恢复百炼沙箱", exc, api_key_env=self._api_key_env) from exc

    def kill(self) -> None:
        handle = self._require_handle()
        try:
            handle.kill()
        except Exception as exc:  # noqa: BLE001
            raise _translate("终止百炼沙箱", exc, api_key_env=self._api_key_env) from exc

    def status(self) -> SandboxStatus:
        """只读探测 provider 侧状态（**不抛错**：不可用即如实回报，AC-002-4 的输入）。"""
        if self._handle is None:
            return SandboxStatus(
                sandbox_id=self.sandbox_id, phase="none", available=False, detail="尚未 provision"
            )
        try:
            info = self._handle.get_info()
        except SandboxNotFoundException:
            return SandboxStatus(
                sandbox_id=self.sandbox_id,
                phase="crashed",
                available=False,
                detail="沙箱不存在或已被回收",
            )
        except Exception as exc:  # noqa: BLE001 —— 探测失败也要结构化回报
            return SandboxStatus(
                sandbox_id=self.sandbox_id,
                phase="crashed",
                available=False,
                detail=f"探测失败：{type(exc).__name__}",
            )
        state = str(getattr(info, "state", "") or "")
        if state == "running":
            return SandboxStatus(sandbox_id=self.sandbox_id, phase="running", available=True)
        if state == "paused":
            return SandboxStatus(sandbox_id=self.sandbox_id, phase="paused", available=True)
        return SandboxStatus(
            sandbox_id=self.sandbox_id,
            phase="crashed",
            available=False,
            detail=f"未知沙箱状态：{state!r}",
        )

    # ------------------------------------------------------------------- 协议原语

    def _execute_command(
        self, command: str, *, timeout: int | None, exec_id: str | None
    ) -> ExecuteResponse:
        handle = self._require_handle()

        def _on_stdout(chunk: str) -> None:
            self.emit_output_text("stdout", chunk, exec_id)

        def _on_stderr(chunk: str) -> None:
            self.emit_output_text("stderr", chunk, exec_id)

        try:
            result = handle.commands.run(
                command,
                timeout=timeout or self._default_timeout_seconds,
                on_stdout=_on_stdout,
                on_stderr=_on_stderr,
            )
        except CommandExitException as exc:
            # 非零退出是**命令的正常结果**（如非 git 目录下 `git rev-parse` exit 128），
            # 不是沙箱故障：CommandExitException 继承 CommandResult，直接映射回响应。
            # 流式块已在 run 迭代期间实时发出，这里不重复 emit。
            return ExecuteResponse(
                output=combine_output(exc.stdout or "", exc.stderr or ""),
                exit_code=exc.exit_code,
                truncated=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate("执行沙箱命令", exc, api_key_env=self._api_key_env) from exc
        return ExecuteResponse(
            output=combine_output(result.stdout or "", result.stderr or ""),
            exit_code=result.exit_code,
            truncated=False,
        )

    def _write_file(self, path: str, content: bytes) -> None:
        handle = self._require_handle()
        try:
            handle.files.write(path, content)
        except FileNotFoundException as exc:
            raise SandboxFileError(FILE_NOT_FOUND, path, detail=_short(exc)) from exc
        except IsADirectoryError as exc:
            raise SandboxFileError(IS_DIRECTORY, path, detail=_short(exc)) from exc
        except FileNotFoundError as exc:  # SDK 也可能透出内建异常，同样归一化
            raise SandboxFileError(FILE_NOT_FOUND, path, detail=_short(exc)) from exc
        except PermissionError as exc:
            raise SandboxFileError(PERMISSION_DENIED, path, detail=_short(exc)) from exc
        except InvalidArgumentException as exc:
            raise SandboxFileError("invalid_path", path, detail=_short(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxFileError(_describe_file_error(exc), path, detail=_short(exc)) from exc

    def _read_file(self, path: str) -> bytes:
        handle = self._require_handle()
        try:
            raw = handle.files.read(path, format="bytes")
        except FileNotFoundException as exc:
            raise SandboxFileError(FILE_NOT_FOUND, path, detail=_short(exc)) from exc
        except IsADirectoryError as exc:
            raise SandboxFileError(IS_DIRECTORY, path, detail=_short(exc)) from exc
        except FileNotFoundError as exc:
            raise SandboxFileError(FILE_NOT_FOUND, path, detail=_short(exc)) from exc
        except PermissionError as exc:
            raise SandboxFileError(PERMISSION_DENIED, path, detail=_short(exc)) from exc
        except InvalidArgumentException as exc:
            raise SandboxFileError("invalid_path", path, detail=_short(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxFileError(_describe_file_error(exc), path, detail=_short(exc)) from exc
        return bytes(raw)

    # ------------------------------------------------------------------------ 内部

    def _require_handle(self) -> Any:
        if self._handle is None:
            raise SandboxLifecycleError("百炼沙箱尚未 provision（先调用 create()）")
        return self._handle


def _translate(context: str, exc: Exception, *, api_key_env: str) -> SandboxError:
    """e2b 异常 → LCode 沙箱异常（消息只含类型名；明细进 `detail`，不进事件/响应）。"""
    if isinstance(exc, AuthenticationException):
        return SandboxConfigurationError(
            f"{context}失败：百炼鉴权被拒（检查环境变量 "
            f"{safe_env_var_name(api_key_env)} 指向的密钥）",
            detail=_short(exc),
            original_exception=exc,
        )
    if isinstance(exc, TimeoutException):
        return SandboxUnavailable(
            f"{context}失败：百炼沙箱超时（可能已休眠/被回收）",
            detail=_short(exc),
            original_exception=exc,
        )
    if isinstance(exc, SandboxNotFoundException):
        return SandboxUnavailable(
            f"{context}失败：沙箱不存在或已被回收",
            detail=_short(exc),
            original_exception=exc,
        )
    return SandboxUnavailable(
        f"{context}失败：{type(exc).__name__}",
        detail=_short(exc),
        original_exception=exc,
    )


def _describe_file_error(exc: Exception) -> str:
    """未知文件错误 → 不做归一化猜测：带类型名由上层如实呈现。"""
    return f"{type(exc).__name__}"


def _short(exc: Exception, limit: int = 300) -> str:
    """异常文本截断（进 detail 供排查；密钥不会出现在 e2b 的异常文本里）。"""
    return str(exc)[:limit]


__all__ = ["DEFAULT_SANDBOX_TIMEOUT_SECONDS", "BailianSandbox"]
