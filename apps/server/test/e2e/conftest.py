"""e2e 层 conftest：子进程启动真服务（独立端口 18000），轮询就绪后开跑，session 结束清理。

接入数据库后，在 pytest_sessionstart 里先重建测试库并 `alembic upgrade head`
再启动服务（完整版引导见 references/testing.md 第五节）。

清理注意：服务以独立进程组启动（start_new_session），收尾先 SIGTERM 整组、收尸主进程，
再探测组内残留成员补 SIGKILL，连带 uv / uvicorn reload 子进程，避免残留进程占用 18000 端口。
（POSIX 方案；Windows 改用 _process.terminate()/kill()）
"""

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

BASE_URL = "http://127.0.0.1:18000"
# conftest 位于 apps/<svc>/test/e2e/ 下，parents[4] 即仓库根
# （也是服务子进程的 cwd，决定其 .env 解析位置——所有命令统一从仓库根跑）
ROOT = Path(__file__).resolve().parents[4]

# 装载 .env.test：值进入 os.environ 后被服务子进程继承（Popen 默认继承父进程环境）
load_dotenv(ROOT / ".env.test")

# 日志文件隔离：.env.test 存在时以它的 LOG_FILE_DIR=tmp/logs 为准；被 cp 模板遗漏或
# 本地 .env.test 是旧版（无 LOG_ 组）时兜底 tmp/logs——测试期故意触发的异常不把错误
# 日志写进仓库根（tmp/ 已 gitignore）。子进程经继承拿到同值。
os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

_process: subprocess.Popen | None = None


def _signal_group(pgid: int, sig: int) -> None:
    """对进程组发信号；组已消失（ESRCH）或只剩待收尸的僵尸时安静跳过。

    macOS 注意：killpg 对「组内已无存活、仅剩未收尸僵尸」的组返回 EPERM
    （errno 1），与 ESRCH 一样属于不可/无需再终止的状态，一并忽略。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    """探测进程组是否仍有可终止的存活成员（信号 0 只做存在性检查）。

    EPERM（macOS 对僵尸组的行为，见 _signal_group 注释）视为"无存活成员"。
    用 `contextlib.suppress` 而非 `except`：显式元组参数，避免 PEP 758 的无括号
    多异常形式（`except A, B:` 在 3.14 合法但形似 py2 语法，易被误读者/工具误判）；
    与 `_signal_group` 保持同一风格。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, 0)
        return True

    return False


def _stop_service() -> None:
    """整组终止服务：SIGTERM 优雅退出，10 秒未退再 SIGKILL。

    即使主进程已退出也继续清理：uvicorn reload 的子进程可能成为孤儿仍占端口，
    而进程组 ID 始终等于启动时的主进程 PID（start_new_session=True），
    只要组内还有存活成员，killpg 依然有效。先收尸主进程再探测/补刀，
    避免其僵尸状态干扰信号 0 探测。
    """
    global _process
    if _process is None:
        return
    pgid = _process.pid

    _signal_group(pgid, signal.SIGTERM)

    with contextlib.suppress(subprocess.TimeoutExpired):
        _process.wait(timeout=10)  # 收尸主进程，同时给优雅退出留时间窗

    if _group_alive(pgid):  # 组内仍有存活成员（孤儿 reload 子进程等）→ 补刀
        _signal_group(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            _process.wait(timeout=5)

    _process = None


def pytest_sessionstart(session):
    """启动真服务子进程并等待 /health 就绪（超时 60s 报错并清理）。

    `--env-file .env.test`：给子进程传**测试环境参数**（`ENVIRONMENT=test` 与
    `DB_*` 测试库名/连接池等）。契约包 `lcode_contracts` 由共享 venv 的 editable
    安装解析（workspace 成员），因此真服务的 `import` 不依赖任何路径环境变量——
    与 unit/integration 的 import 语义一致（不传 `--env-file` 也能起服务，
    只是会读默认配置）。
    """
    global _process
    _process = subprocess.Popen(
        [
            "uv",
            "run",
            "--package",
            "lcode-server",
            "--env-file",
            ".env.test",
            "fastapi",
            "dev",
            "apps/server/app/main.py",
            "--port",
            "18000",
            "--no-reload",  # e2e 不需要热重载：无 reloader 中间层，SIGTERM 收尾确定、少一层子进程
        ],
        cwd=ROOT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)  # 统一退避：连接失败与非 200（如 5xx）一律等待，避免全速空转
    _stop_service()
    raise RuntimeError("e2e 服务启动超时：/health 未在 60s 内就绪")


def pytest_sessionfinish(session, exitstatus):
    _stop_service()


@pytest.fixture
def base_url() -> str:
    """真服务基地址（e2e 测试统一经它发请求）。"""
    return BASE_URL
