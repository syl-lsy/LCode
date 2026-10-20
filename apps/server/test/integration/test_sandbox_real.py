"""integration：真沙箱冒烟（T8）——缺凭证显式 skip 并给 reason（test-plan-m2 §1.4）。

用例清单（AC 映射见 §2.2 / §2.6）：

- `requires_sandbox`（真百炼，E2B 协议）：
  - `test_bailian_create_execute_and_stream`（AC-002-1 的真沙箱版）
  - `test_bailian_pause_resume_keeps_filesystem`（AC-002-2 真沙箱版）
  - `test_bailian_q7_toolchain_present`（Q7 三件套实测留痕；git/python3/coreutils）
  - `test_bailian_non_git_input_baseline`（AC-002-5 真沙箱版）
- `requires_sandbox_opensandbox`（自托管 OpenSandbox，可延后至实际切备选，U4）：
  - `test_opensandbox_smoke`

skip 策略：conftest 不感知沙箱凭证（test/integration/conftest.py 只管 DB），因此每个
用例开头调 `*_ready()`，不满足就 `pytest.skip(reason)`——reason 里点名缺的**配置项或
环境变量名**（绝不回显密钥值，NFR-004）。标记本身只用于 `-m` 过滤与 CI 报表。

注意（M2 出口口径，test-plan §1.4）：真百炼冒烟**至少人工跑通 1 轮并留痕**——本机当前
未配置 `BAILIAN_API_KEY`，所以这里的用例在本环境全部显式 skip；`scripts/spike_bailian_
sandbox.py` 是同一套探针的独立入口（Q7 spike）。
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
def test_bailian_missing_key_skips_cleanly_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺凭证的报错面：SandboxConfigurationError 只含变量名（NFR-004，直接断言不连远端）。"""
    from app.sandbox.base import SandboxConfigurationError as ConfigError

    monkeypatch.delenv(sandbox_settings.bailian_api_key_env, raising=False)
    client = BailianSandbox(settings=sandbox_settings)

    with pytest.raises(ConfigError) as excinfo:
        client.create()

    assert sandbox_settings.bailian_api_key_env in str(excinfo.value)


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


# --------------------------------------------------------------------------- 配置面


def test_bailian_configuration_error_message_contains_no_key_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺凭证错误只点名变量名（NFR-004）：无凭证配置下直接断言，不连远端（永远跑）。"""
    monkeypatch.delenv("LANE_NEVER_SET_KEY_ENV", raising=False)
    empty_cfg = SandboxSettings(bailian_api_key_env="LANE_NEVER_SET_KEY_ENV")
    client = BailianSandbox(settings=empty_cfg)

    with pytest.raises(SandboxConfigurationError) as excinfo:
        client.create()

    message = str(excinfo.value)
    assert "LANE_NEVER_SET_KEY_ENV" in message
    assert "凭证" in message or "密钥" in message
