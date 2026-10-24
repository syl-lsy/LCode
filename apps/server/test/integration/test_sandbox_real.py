"""integration：真沙箱冒烟（T8）——缺凭证显式 skip 并给 reason（test-plan-m2 §1.4）。

用例清单（AC 映射见 §2.2 / §2.6）：

- `requires_sandbox`（真百炼，E2B 协议）：
  - `test_bailian_create_execute_and_stream`（AC-002-1 的真沙箱版）
  - `test_bailian_pause_resume_keeps_filesystem`（AC-002-2 真沙箱版）
  - `test_bailian_q7_toolchain_present`（Q7 三件套实测留痕；git/python3/coreutils）
  - `test_bailian_non_git_input_baseline`（AC-002-5 真沙箱版）
- `requires_sandbox_opensandbox`（自托管 OpenSandbox，可延后至实际切备选，U4）：
  - `test_opensandbox_smoke`
- 配置面（无凭证、无远端调用，任何环境都跑）：
  - `test_bailian_missing_key_raises_configuration_error_without_remote_call`
  - `test_bailian_configuration_error_message_contains_no_key_value`

skip 策略：conftest 不感知沙箱凭证（test/integration/conftest.py 只管 DB），因此每个
真沙箱用例开头调 `*_ready()`，不满足就 `pytest.skip(reason)`——reason 里点名缺的**配置
项或环境变量名**（绝不回显密钥值，NFR-004）。标记本身只用于 `-m` 过滤与 CI 报表。
配置面用例不调 `*_ready()`、也不打真沙箱标记：它们只验证校验顺序与消息面，显式构造
配置（`_CONFIG_ONLY_API_URL` 假地址）以不依赖本地 `.env` 的 URL / 凭证（CI 无 `.env`）。

注意（M2 出口口径，test-plan §1.4）：真百炼冒烟**至少人工跑通 1 轮并留痕**——CI 无
凭证（`.env.test` 由 `.env.test.example` 生成，`SANDBOX_BAILIAN_API_URL` 为空）时这些
用例显式 skip；要在本机真跑，需在 `.env.test` 配好 `BAILIAN_API_KEY` /
`SANDBOX_BAILIAN_API_URL` / `SANDBOX_BAILIAN_TEMPLATE`。`scripts/spike_bailian_sandbox.py`
是同一套探针的独立入口（Q7 spike）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.core.config import SandboxSettings, safe_env_var_name, sandbox_settings
from app.sandbox.bailian import BailianSandbox
from app.sandbox.base import (
    LcodeSandbox,
    SandboxConfigurationError,
    SandboxOutputChunk,
    probe_required,
)
from app.sandbox.opensandbox import OpenSandboxSandbox

_CONFIG_ONLY_API_URL = "https://example.invalid/api/v1"
"""配置面用例的假 `bailian_api_url`：只为越过/触发 URL fail-fast，绝不发起网络请求。

`.invalid` 是 RFC 2606 保留 TLD（永不解析），即使被测代码误发请求也会立刻失败而非打到
真实端点；同时避免用例隐式依赖本地 `.env` 的 `SANDBOX_BAILIAN_API_URL`（CI 为空）。
"""

# --------------------------------------------------------------------------- 就绪判定


def _bailian_ready(cfg: SandboxSettings | None = None) -> tuple[bool, str]:
    """真百炼冒烟的前置：密钥环境变量非空 + 模板已配置 + api_url 已填写（spike 产出）。"""
    settings = cfg or sandbox_settings
    key = os.environ.get(settings.bailian_api_key_env, "").strip()
    if not key:
        return (
            False,
            f"缺凭证：环境变量 {safe_env_var_name(settings.bailian_api_key_env)} 未设置"
            "（requires_sandbox 显式跳过，test-plan §1.4）",
        )
    if not settings.bailian_template:
        return (
            False,
            "缺配置：SANDBOX_BAILIAN_TEMPLATE 为空（M2 spike 定镜像模板后填 .env.test）",
        )
    if not settings.bailian_api_url:
        return (
            False,
            "缺配置：SANDBOX_BAILIAN_API_URL 为空（百炼 E2B 接入完整地址）",
        )
    return True, ""


def _opensandbox_ready(cfg: SandboxSettings | None = None) -> tuple[bool, str]:
    """真 OpenSandbox 冒烟的前置：endpoint + 密钥 + 镜像（镜像读 settings 配置项）。"""
    settings = cfg or sandbox_settings
    if not settings.opensandbox_endpoint:
        return False, "缺配置：SANDBOX_OPENSANDBOX_ENDPOINT 为空（自托管 OpenSandbox 未部署）"
    if not os.environ.get(settings.opensandbox_api_key_env, "").strip():
        return (
            False,
            f"缺凭证：环境变量 {safe_env_var_name(settings.opensandbox_api_key_env)} 未设置"
            "（requires_sandbox_opensandbox 显式跳过）",
        )
    if not settings.opensandbox_image.strip():
        return (
            False,
            "缺配置：SANDBOX_OPENSANDBOX_IMAGE 未设置（OpenSandbox 镜像；"
            "SandboxSync.create 要求 image，本适配器不支持 snapshot）",
        )
    return True, ""


def _skip_unless(ready: tuple[bool, str]) -> None:
    if not ready[0]:
        pytest.skip(ready[1])


# --------------------------------------------------------------------------- 百炼冒烟


@pytest.fixture
def bailian_sandbox() -> Iterator[LcodeSandbox]:
    """真百炼沙箱（requires_sandbox 用例专用）：测试结束确保 kill，不留计费实例。"""
    _skip_unless(_bailian_ready())
    client = BailianSandbox(default_timeout_seconds=900)
    client.create(timeout_seconds=900)
    yield client
    try:
        client.kill()
    except Exception:  # noqa: BLE001 —— 清理失败如实提示，不掩盖测试结果
        print(f"[cleanup] 沙箱 kill 失败，可能需要控制台手工终止：{client.id}")


@pytest.mark.requires_sandbox
def test_bailian_create_execute_and_stream(bailian_sandbox: LcodeSandbox) -> None:
    """AC-002-1 真沙箱版：创建 → 执行 → 流式块带 exec_id → 结果含退出码。"""
    chunks: list[SandboxOutputChunk] = []
    bailian_sandbox.add_output_handler(chunks.append)

    result = bailian_sandbox.run_command("echo lcode-smoke", exec_id="smoke-exec-1")

    assert result.exit_code == 0
    assert "lcode-smoke" in result.output
    streamed = [(chunk.stream, chunk.data) for chunk in chunks]
    assert any(stream == "stdout" and "lcode-smoke" in data for stream, data in streamed)
    assert {chunk.exec_id for chunk in chunks} == {"smoke-exec-1"}


@pytest.mark.requires_sandbox
def test_bailian_pause_resume_keeps_filesystem(bailian_sandbox: LcodeSandbox) -> None:
    """AC-002-2 真沙箱版：pause → resume 后文件仍在（硬约束 8 的显式 pause 语义）。"""
    uploads = bailian_sandbox.upload_files([("lcode-smoke.txt", b"persist-me\n")])
    assert uploads[0].error is None

    bailian_sandbox.pause()
    bailian_sandbox.resume()

    downloads = bailian_sandbox.download_files(["lcode-smoke.txt"])
    assert downloads[0].error is None
    assert downloads[0].content == b"persist-me\n"


@pytest.mark.requires_sandbox
def test_bailian_q7_toolchain_present(bailian_sandbox: LcodeSandbox) -> None:
    """Q7 三件套实测：git/python3/coreutils 任一缺失即红（设计 §4.4：缺 git 必须装）。"""
    probes = bailian_sandbox.probe_toolchain()

    missing = probe_required(probes)
    detail = "; ".join(
        f"{name} -> exit={probe.exit_code} out={probe.output!r}" for name, probe in probes.items()
    )
    assert not missing, f"镜像缺工具链 {missing}（Q7）：{detail}"


@pytest.mark.requires_sandbox
def test_bailian_non_git_input_baseline(bailian_sandbox: LcodeSandbox) -> None:
    """AC-002-5 真沙箱版：非 git 输入 → 契约渲染的 init 提交 + round-0 tag。"""
    from lcode_contracts.baseline import BaselineCommitMessage, parse_tag, tag_for_round

    assert bailian_sandbox.is_git_repository() is False
    init = bailian_sandbox.create_init_baseline(note="smoke")

    assert init.message == BaselineCommitMessage(kind="init", round=0, note="smoke").render()
    assert init.tag == tag_for_round(0)
    assert parse_tag(init.tag).kind == "init"

    verify = bailian_sandbox.run_command("git log -1 --pretty=%s && git tag --list")
    assert "[lcode-baseline] init: smoke" in verify.output
    assert "lcode-baseline/round-0" in verify.output


@pytest.mark.requires_sandbox
def test_bailian_restart_reconnect_by_id() -> None:
    """S3 adopt 真沙箱版：create → 记 id → 全新适配器按 id adopt → 文件仍在。

    模拟进程重启：第一只实例只负责 create + 写文件，**不 kill**（沙箱留在 provider 侧，
    靠 timeout 到期自然回收）；第二只全新适配器经 `adopt(sandbox_id)` 重连同一沙箱
    （e2b 语义：running 直连 / paused 自动恢复并刷新 timeout）。清理时只 kill 一次
    （重连实例与原实例指向同一沙箱，双重 kill 会报「不存在」）。
    """
    _skip_unless(_bailian_ready())
    first = BailianSandbox(default_timeout_seconds=900)
    adopted: BailianSandbox | None = None
    try:
        first.create(timeout_seconds=900)
        uploads = first.upload_files([("lcode-adopt.txt", b"adopt-me\n")])
        assert uploads[0].error is None
        sandbox_id = first.id

        adopted = BailianSandbox(default_timeout_seconds=900)
        adopted.adopt(sandbox_id)
        assert adopted.id == sandbox_id

        downloads = adopted.download_files(["lcode-adopt.txt"])
        assert downloads[0].error is None
        assert downloads[0].content == b"adopt-me\n"
    finally:
        victim = adopted if adopted is not None else first
        try:
            victim.kill()
        except Exception:  # noqa: BLE001 —— 清理失败如实提示，不掩盖测试结果
            print(f"[cleanup] 沙箱 kill 失败，可能需要控制台手工终止：{victim.id}")


def test_bailian_missing_key_raises_configuration_error_without_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺凭证的报错面：SandboxConfigurationError 只含变量名（NFR-004，配置面用例不连远端）。

    显式给出 `bailian_api_url`（假地址）以越过 URL fail-fast，让断言落在凭证校验上；
    不读全局 `sandbox_settings` 的 URL，避免隐式依赖本地 `.env`（CI 无 `.env`、URL 为空）。
    """
    from app.sandbox.base import SandboxConfigurationError as ConfigError

    monkeypatch.delenv(sandbox_settings.bailian_api_key_env, raising=False)
    config_only = SandboxSettings(
        bailian_api_key_env=sandbox_settings.bailian_api_key_env,
        bailian_api_url=_CONFIG_ONLY_API_URL,
    )
    client = BailianSandbox(settings=config_only)

    with pytest.raises(ConfigError) as excinfo:
        client.create()

    assert config_only.bailian_api_key_env in str(excinfo.value)


# --------------------------------------------------------------------- OpenSandbox 冒烟


@pytest.mark.requires_sandbox_opensandbox
def test_opensandbox_smoke() -> None:
    """AC-002-6 备选 provider 真冒烟（可延后至实际切备选时，U4 / test-plan §1.4）。"""
    _skip_unless(_opensandbox_ready())
    client = OpenSandboxSandbox(settings=sandbox_settings, default_timeout_seconds=900)
    try:
        client.create(timeout_seconds=900)

        result = client.run_command("echo opensandbox-smoke", exec_id="smoke-exec-os")
        assert result.exit_code == 0
        assert "opensandbox-smoke" in result.output

        client.pause()
        client.resume()
        downloads = client.download_files(["/etc/hostname"])
        assert downloads[0].error is None
    finally:
        try:
            client.kill()
        except Exception:  # noqa: BLE001
            print(f"[cleanup] OpenSandbox kill 失败，可能需要手工终止：{client.id}")


@pytest.mark.requires_sandbox_opensandbox
def test_opensandbox_restart_reconnect_by_id() -> None:
    """S3 adopt 真沙箱版（评审项 1 的真回归锁）：create → 记 id → 全新适配器按 id adopt → 文件仍在。

    adopt 走 `SandboxSync.connect`（按 id 连接，无状态迁移）——对 **running** 态沙箱
    重连（进程重启后最常见的形态）；resume 的服务端 paused 语义不在本用例路径上。
    清理时只 kill 一次（重连实例与原实例指向同一沙箱）。
    """
    _skip_unless(_opensandbox_ready())
    first = OpenSandboxSandbox(settings=sandbox_settings, default_timeout_seconds=900)
    adopted: OpenSandboxSandbox | None = None
    try:
        first.create(timeout_seconds=900)
        uploads = first.upload_files([("lcode-adopt.txt", b"adopt-me\n")])
        assert uploads[0].error is None
        sandbox_id = first.id

        adopted = OpenSandboxSandbox(settings=sandbox_settings, default_timeout_seconds=900)
        adopted.adopt(sandbox_id)
        assert adopted.id == sandbox_id

        downloads = adopted.download_files(["lcode-adopt.txt"])
        assert downloads[0].error is None
        assert downloads[0].content == b"adopt-me\n"
    finally:
        victim = adopted if adopted is not None else first
        try:
            victim.kill()
        except Exception:  # noqa: BLE001 —— 清理失败如实提示，不掩盖测试结果
            print(f"[cleanup] OpenSandbox kill 失败，可能需要手工终止：{victim.id}")


# --------------------------------------------------------------------------- 配置面


def test_bailian_configuration_error_message_contains_no_key_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺凭证错误只点名变量名（NFR-004）：无凭证配置下直接断言，不连远端（永远跑）。"""
    monkeypatch.delenv("LANE_NEVER_SET_KEY_ENV", raising=False)
    empty_cfg = SandboxSettings(
        bailian_api_key_env="LANE_NEVER_SET_KEY_ENV",
        bailian_api_url=_CONFIG_ONLY_API_URL,
    )
    client = BailianSandbox(settings=empty_cfg)

    with pytest.raises(SandboxConfigurationError) as excinfo:
        client.create()

    message = str(excinfo.value)
    assert "LANE_NEVER_SET_KEY_ENV" in message
    assert "凭证" in message or "密钥" in message
