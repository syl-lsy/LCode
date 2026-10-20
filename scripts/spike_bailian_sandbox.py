#!/usr/bin/env python3
"""Q7 spike：百炼镜像三件套（git / python3 / coreutils）探针（PRD 开放问题 Q7）。

背景（backend-design §4.4 / U5）：BaseSandbox 的文件工具依赖 POSIX sh + coreutils +
python3（基类经命令执行派生），diff 链与 round-0 兜底基线依赖 git——**镜像是否预装
必须实测**，缺失则在模版构建阶段安装。

运行方式（从仓库根）：

    uv run python scripts/spike_bailian_sandbox.py

行为约定（按用户约束）：
- **只在凭证存在时连真沙箱**：读 `SANDBOX_BAILIAN_API_KEY_ENV` 指向的环境变量；缺失时
  打印明确跳过信息并 exit 0（不猜测、不连假端点）；
- `SANDBOX_BAILIAN_TEMPLATE` 为空时给出警告（将退到 E2B 协议默认模板，百炼可能没有）；
- 探针经 `LcodeSandbox.probe_toolchain()`（与 `integration/test_sandbox_real.py::
  test_bailian_q7_toolchain_present` 同一套判据），结果逐条打印；
- 无论成败，finally 里尝试 kill 沙箱（清理失败如实提示，不静默）；
- 密钥只从环境变量读取，**不打印密钥本体**（NFR-004）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 脚本从仓库根以 `uv run python scripts/...` 运行：把服务包目录挂进 sys.path，
# 使 `import app.*` 与 `uv run --package lcode-server` 同源（不复制任何业务代码）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "server"))

from app.core.config import sandbox_settings  # noqa: E402 —— 依赖上面的 sys.path 引导
from app.sandbox.bailian import BailianSandbox  # noqa: E402
from app.sandbox.base import (  # noqa: E402 —— 依赖上面的 sys.path 引导
    SandboxConfigurationError,
    SandboxError,
    SandboxUnavailable,
    probe_required,
)


def main() -> int:
    key_env = sandbox_settings.bailian_api_key_env
    api_key = os.environ.get(key_env, "").strip()

    if not api_key:
        print(
            f"[skip] 缺少百炼沙箱凭证：环境变量 {key_env} 未设置——未连接真沙箱（Q7 探针需要凭证）。"
        )
        print(
            f"[skip] 配置方法：export {key_env}=<百炼密钥> 并设置"
            " SANDBOX_BAILIAN_TEMPLATE=<镜像模板> 后重跑。"
        )
        return 0

    if not sandbox_settings.bailian_template:
        print(
            "[warn] SANDBOX_BAILIAN_TEMPLATE 为空：将使用 E2B 协议默认模板"
            "（百炼可能没有该模板，建议显式配置）。"
        )

    sandbox = BailianSandbox(default_timeout_seconds=600)
    try:
        sandbox.create(timeout_seconds=600)
        print(f"[ok] 沙箱已创建：{sandbox.id}（provider=bailian）")

        probes = sandbox.probe_toolchain()
        for name, probe in probes.items():
            status = "present" if probe.present else "MISSING"
            first_line = probe.output.splitlines()[:1]
            detail = (
                first_line[0]
                if first_line
                else (f"exit={probe.exit_code}" if probe.exit_code is not None else "no output")
            )
            print(f"[Q7] {name:<10s} {status:<8s} $ {probe.command!r} -> {detail}")

        missing = probe_required(probes)
        if missing:
            print(
                f"[Q7] 结论：缺失 {missing} —— 按设计需在模版构建阶段安装"
                "（backend-design §4.4 / U5）。"
            )
            return 1
        print(
            "[Q7] 结论：三件套（git / python3 / coreutils）齐备，"
            "BaseSandbox 文件工具与 diff 链可用。"
        )
        return 0
    except SandboxConfigurationError as exc:
        print(f"[error] 配置不完整：{exc}")
        return 2
    except SandboxUnavailable as exc:
        print(f"[error] 沙箱不可用：{exc}（detail={exc.detail}）")
        return 2
    finally:
        try:
            sandbox.kill()
            print("[cleanup] 沙箱已终止。")
        except SandboxError as exc:
            print(
                f"[cleanup] kill 失败（{exc.message}）："
                f"沙箱 {sandbox.id} 可能需要到控制台手工终止。"
            )


if __name__ == "__main__":
    raise SystemExit(main())
