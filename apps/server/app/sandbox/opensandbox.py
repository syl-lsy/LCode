"""OpenSandbox 适配器（自托管备选；**自有协议，非 E2B 兼容**——AGENTS.md 硬约束 12）。

版本线：`opensandbox==1.1.1`（1.1.0 起统一版本治理，但**尚无稳定 v1 API 承诺**）
→ 适配层锁版本，升级必须过本目录回归（`unit/test_sandbox_manager.py` 的适配器段
+ `integration/test_sandbox_real.py::test_opensandbox_*`）。

对齐面（A3 §4.2 的 OpenSandbox 列）：

- `_execute_command` → `SandboxSync.commands.run(cmd, opts=RunCommandOpts(timeout=…),
  handlers=ExecutionHandlersSync(on_stdout=…, on_stderr=…))`；
- `_write_file` → `SandboxSync.files.write_file(path, data)`；
- `_read_file` → `SandboxSync.files.read_bytes(path)`；
- `create` → `SandboxSync.create(image, timeout=timedelta(…),
  connection_config=ConnectionConfigSync(...))`；
- `pause`/`resume`/`kill` → `SandboxSync.pause()` / **类方法**
  `SandboxSync.resume(sandbox_id, connection_config=…)`（返回新实例，
  端点在 pause/resume 间可能变化）/ `SandboxSync.kill()`；
- `status` → `SandboxSync.get_info().status.state`（RUNNING/PENDING/PAUSED/TERMINATED）。

流式能力（U4 未决）：本适配器**已按流式接线**——`ExecutionHandlersSync.on_stdout/on_stderr`
是 SDK 的 SSE 回调（安装版 `sync/adapters/converter/execution_event_dispatcher.py::_handle_stdout`
逐事件回调），故 `sandbox_output` 走真流；若某后端退化为命令结束单块，`RunCommandOpts`
与回调语义不变，属服务端行为差异（NFR-005 已给豁免注记）。

两处配置口径：
1. **镜像解析优先级：构造参数 > `settings.opensandbox_image`**（`SandboxSync.create` 要求
   `image` 或 `snapshot_id` 二者之一；本适配器不支持 snapshot）。二者皆空时 create 抛
   `SandboxConfigurationError`（消息点名配置项 `SANDBOX_OPENSANDBOX_IMAGE`），**不硬编码
   镜像、也不私读未登记的 env**；
2. `SANDBOX_OPENSANDBOX_BACKEND`（docker/k8s/firecracker）是**服务端部署属性**，
   客户端不传参，故本适配器不消费它（写进 `status().detail` 以外的任何地方都是误导）。

测试缝隙：`sandbox_factory` / `resume_factory` 可注入假 SDK（unit 层不连真 endpoint）；
真冒烟走 `integration/test_sandbox_real.py` 的 `requires_sandbox_opensandbox` 标记。
SDK 句柄以 `Any` 承载（生成式类型面不并入本仓库 mypy 门禁，适配器保持薄翻译）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from deepagents.backends.protocol import FILE_NOT_FOUND, ExecuteResponse
from opensandbox import SandboxSync
from opensandbox.config import ConnectionConfigSync
from opensandbox.exceptions import (
    InvalidArgumentException,
    SandboxConnectionException,
    SandboxException,
    SandboxReadyTimeoutException,
    SandboxTimeoutException,
    SandboxUnhealthyException,
)
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.execd_sync import ExecutionHandlersSync

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
"""默认生命周期（秒）：与百炼适配器同口径（1 小时），空闲成本靠显式 pause 控制。"""


class OpenSandboxSandbox(LcodeSandbox):
    """自托管 OpenSandbox 沙箱：`provider="opensandbox"`（自有协议）。"""

    provider = "opensandbox"

    def __init__(
        self,
        *,
        session_id: str | None = None,
        settings: SandboxSettings | None = None,
        image: str | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
        resume_factory: Callable[..., Any] | None = None,
        default_timeout_seconds: int = DEFAULT_SANDBOX_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(session_id=session_id)
        cfg = settings or sandbox_settings
        self._endpoint = cfg.opensandbox_endpoint
        self._api_key_env = cfg.opensandbox_api_key_env
        # 显式构造参数优先于 settings（都为空时由 _require_image 在 create 时 fail-fast）
        self._image = (image or cfg.opensandbox_image).strip()
        self._default_timeout_seconds = default_timeout_seconds
        # 可注入替身：`SandboxSync.create` / 类方法 `SandboxSync.resume`
        self._sandbox_factory: Callable[..., Any] = sandbox_factory or SandboxSync.create
        self._resume_factory: Callable[..., Any] = resume_factory or SandboxSync.resume
        self._handle: Any = None

    # ------------------------------------------------------------------ 凭证与连接

    def _require_api_key(self) -> str:
        """从 `*_API_KEY_ENV` 指向的环境变量读密钥；缺失时消息**只含变量名**。"""
        env_name = safe_env_var_name(self._api_key_env)
        value = os.environ.get(self._api_key_env, "").strip()
        if not value:
            raise SandboxConfigurationError(
                f"缺少 OpenSandbox 凭证：环境变量 {env_name} 未设置或为空"
                "（密钥只从环境变量读取，见 NFR-004）"
            )
        return value

    def _require_endpoint(self) -> str:
        if not self._endpoint:
            raise SandboxConfigurationError(
                "缺少 OpenSandbox endpoint：配置项 SANDBOX_OPENSANDBOX_ENDPOINT 为空"
                "（自托管部署地址，按实际端口填写）"
            )
        return self._endpoint

    def _require_image(self) -> str:
        if not self._image:
            raise SandboxConfigurationError(
                "缺少 OpenSandbox 镜像：配置项 SANDBOX_OPENSANDBOX_IMAGE 为空"
                "（SandboxSync.create 要求 image 或 snapshot_id；本适配器不支持 snapshot，"
                "须在 .env / .env.test 配置镜像，或显式传构造参数 image；不内置默认镜像）"
            )
        return self._image

    def _connection_config(self) -> ConnectionConfigSync:
        """连接配置：endpoint 既可写 `host:port`（默认 http）也可写完整 URL。"""
        return ConnectionConfigSync(
            api_key=self._require_api_key(), domain=self._require_endpoint()
        )

    # ---------------------------------------------------------------------- 生命周期

    def create(self, *, timeout_seconds: int | None = None) -> None:
        if self._handle is not None:
            raise SandboxLifecycleError("OpenSandbox 沙箱已 provision（create 只能调用一次）")
        timeout = timedelta(seconds=timeout_seconds or self._default_timeout_seconds)
        try:
            handle = self._sandbox_factory(
                self._require_image(), timeout=timeout, connection_config=self._connection_config()
            )
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 —— SDK 异常族统一翻译（见 _translate）
            raise _translate("创建 OpenSandbox 沙箱", exc, api_key_env=self._api_key_env) from exc
        self._handle = handle
        self._set_sandbox_id(str(handle.id))

    def pause(self) -> None:
        handle = self._require_handle()
        try:
            handle.pause()
        except Exception as exc:  # noqa: BLE001
            raise _translate("暂停 OpenSandbox 沙箱", exc, api_key_env=self._api_key_env) from exc

    def resume(self) -> None:
        """恢复：`SandboxSync.resume` 是**类方法**且返回新实例（端点可能变）→ 换句柄。"""
        sandbox_id = self._require_id()
        try:
            self._handle = self._resume_factory(
                sandbox_id, connection_config=self._connection_config()
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate("恢复 OpenSandbox 沙箱", exc, api_key_env=self._api_key_env) from exc

    def kill(self) -> None:
        handle = self._require_handle()
        try:
            handle.kill()
        except Exception as exc:  # noqa: BLE001
            raise _translate("终止 OpenSandbox 沙箱", exc, api_key_env=self._api_key_env) from exc

    def status(self) -> SandboxStatus:
        """只读探测（**不抛错**）：`get_info().status.state` → 四态映射，未识别即不可用。"""
        if self._handle is None:
            return SandboxStatus(
                sandbox_id=self.sandbox_id, phase="none", available=False, detail="尚未 provision"
            )
        try:
            info = self._handle.get_info()
            state = str(getattr(getattr(info, "status", None), "state", "") or "").upper()
        except Exception as exc:  # noqa: BLE001 —— 探测失败也要结构化回报
            return SandboxStatus(
                sandbox_id=self.sandbox_id,
                phase="crashed",
                available=False,
                detail=f"探测失败：{type(exc).__name__}",
            )
        if state == "RUNNING":
            return SandboxStatus(sandbox_id=self.sandbox_id, phase="running", available=True)
        if state == "PAUSED":
            return SandboxStatus(sandbox_id=self.sandbox_id, phase="paused", available=True)
        return SandboxStatus(
            sandbox_id=self.sandbox_id,
            phase="crashed",
            available=False,
            detail=f"沙箱状态不可用：{state or 'unknown'}",
        )

    # ------------------------------------------------------------------- 协议原语

    def _execute_command(
        self, command: str, *, timeout: int | None, exec_id: str | None
    ) -> ExecuteResponse:
        handle = self._require_handle()

        def _on_stdout(message: Any) -> None:
            self.emit_output_text("stdout", getattr(message, "text", "") or "", exec_id)

        def _on_stderr(message: Any) -> None:
            self.emit_output_text("stderr", getattr(message, "text", "") or "", exec_id)

        handlers = ExecutionHandlersSync(on_stdout=_on_stdout, on_stderr=_on_stderr)
        opts = RunCommandOpts(timeout=timedelta(seconds=timeout or self._default_timeout_seconds))
        try:
            execution = handle.commands.run(command, opts=opts, handlers=handlers)
        except Exception as exc:  # noqa: BLE001
            raise _translate("执行沙箱命令", exc, api_key_env=self._api_key_env) from exc

        stdout_text = execution.text
        stderr_parts = [getattr(item, "text", "") or "" for item in execution.logs.stderr]
        error = execution.error
        if error is not None:  # SDK 把执行错误放在 error 字段（不抛异常）
            stderr_parts.append(f"{getattr(error, 'name', '')}: {getattr(error, 'value', '')}")
        return ExecuteResponse(
            output=combine_output(stdout_text, "".join(stderr_parts)),
            exit_code=execution.exit_code,
            truncated=False,
        )

    def _write_file(self, path: str, content: bytes) -> None:
        handle = self._require_handle()
        try:
            handle.files.write_file(path, content)
        except InvalidArgumentException as exc:
            raise SandboxFileError("invalid_path", path, detail=_short(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxFileError(_describe_file_error(exc), path, detail=_short(exc)) from exc

    def _read_file(self, path: str) -> bytes:
        handle = self._require_handle()
        try:
            return bytes(handle.files.read_bytes(path))
        except SandboxException as exc:
            raise SandboxFileError(_describe_file_error(exc), path, detail=_short(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxFileError(_describe_file_error(exc), path, detail=_short(exc)) from exc

    # ------------------------------------------------------------------------ 内部

    def _require_handle(self) -> Any:
        if self._handle is None:
            raise SandboxLifecycleError("OpenSandbox 沙箱尚未 provision（先调用 create()）")
        return self._handle


_UNAVAILABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    SandboxConnectionException,
    SandboxReadyTimeoutException,
    SandboxTimeoutException,
    SandboxUnhealthyException,
)


def _translate(context: str, exc: Exception, *, api_key_env: str) -> SandboxError:
    """SDK 异常 → LCode 沙箱异常（消息只含类型名；明细进 `detail`）。"""
    if isinstance(exc, InvalidArgumentException):
        return SandboxConfigurationError(
            f"{context}失败：参数不被 OpenSandbox 接受（检查 endpoint / 镜像配置）",
            detail=_short(exc),
            original_exception=exc,
        )
    hint = "（OpenSandbox 不可达或未就绪）" if isinstance(exc, _UNAVAILABLE_EXCEPTIONS) else ""
    return SandboxUnavailable(
        f"{context}失败：{type(exc).__name__}{hint}",
        detail=_short(exc),
        original_exception=exc,
    )


def _describe_file_error(exc: Exception) -> str:
    if isinstance(exc, IsADirectoryError):
        return "is_directory"
    if isinstance(exc, FileNotFoundError):
        return FILE_NOT_FOUND
    if isinstance(exc, PermissionError):
        return "permission_denied"
    return f"{type(exc).__name__}"


def _short(exc: Exception, limit: int = 300) -> str:
    return str(exc)[:limit]


def connection_config_probe(
    *, endpoint: str, api_key: str, protocol: str = "http"
) -> ConnectionConfigSync:
    """便捷构造（脚本/测试用）：显式传值，避免散落地拼 endpoint。"""
    return ConnectionConfigSync(api_key=api_key, domain=endpoint, protocol=protocol)


__all__ = [
    "DEFAULT_SANDBOX_TIMEOUT_SECONDS",
    "OpenSandboxSandbox",
    "connection_config_probe",
]
