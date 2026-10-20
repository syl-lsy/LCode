"""e2e 层 conftest：真库 + 子进程真服务（独立端口 18000）+ 假模型/假沙箱开关注入。

M2 演示链装配（test-plan-m2 §3.1/§3.5；backend-design §9.1/§9.2）：

    sessionstart：安全闸 → 重建 e2e 测试库 → `alembic upgrade head` 建十表
    → 注入 e2e 开关（LCODE_E2E_FAKE_LLM / LCODE_E2E_FAKE_SANDBOX /
    LCODE_E2E_CHECKPOINTER，仅测试开关——不进 wire 契约、不进生产 .env.example，
    注记见 .env.test.example）→ 启动服务子进程（test/e2e/server_entry.py，uvicorn
    起真 app；进程内按开关装配 ScriptedChatModel 路由假模型 + FakeSandbox + 真
    AsyncPostgresSaver）→ 轮询 /health/ready（DB 已连通）。

    每测：不需要清库——用例各自创建全新会话（UUID 主键），互不可见。
    sessionfinish：整组 SIGTERM 停服务 → 删 e2e 测试库。

**环境要求**：PATH 上须有 `createdb` / `dropdb`（postgresql-client）。测试库生命周期
走 CLI：连接参数与库名全走 libpq 标准环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD/
PGDATABASE），argv 全字面量、shell=False——PostgreSQL 的 CREATE/DROP DATABASE 标识符
不支持值占位符参数化，python 侧拼 SQL 文本的做法不进本文件；库名固定 `lcode_e2e_test`
（独立于 integration 的 `lcode_test`，互不干扰）。

清理注意：服务以独立进程组启动（start_new_session），收尾先 SIGTERM 整组、收尸主进程，
再探测组内残留成员补 SIGKILL，连带 uvicorn 子进程，避免残留进程占用 18000 端口。
（POSIX 方案；Windows 改用 _process.terminate()/kill()）

环境分级（M2 收口台账 G4 改口径，替代旧「缺库不红」约定）：

- **未配置**（`.env.test` 缺关键 DB_* 变量）→ 全部用例显式 skip（reason 指明
  `cp .env.test.example .env.test`）——干净 checkout 的本地迭代不该被 e2e 红掉；
- **配置了但坏**（库不可达 / createdb·dropdb 失败 / 迁移失败 / 服务起不来）→
  **`pytest.exit` 非 0 终止整层**，禁止转 skip。旧实现把所有失败一律转整层 skip，
  造成「CI 配了坏 DB 也假绿」——G4 就是要杜绝这种假绿（负控：`DB_PORT=59999`
  必须红）。

优先级口径：`load_dotenv(..., override=False)`（显式声明，负控依赖）——真实环境
变量（如 `DB_PORT=59999 make test-e2e`）**优先于** `.env.test` 同名值。
"""

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv

BASE_URL = "http://127.0.0.1:18000"
# conftest 位于 apps/<svc>/test/e2e/ 下，parents[4] 即仓库根
# （也是服务子进程的 cwd，决定其 .env 解析位置——所有命令统一从仓库根跑）
ROOT = Path(__file__).resolve().parents[4]

# 装载 .env.test：值进入 os.environ 后被服务子进程继承（Popen 默认继承父进程环境）。
# override=False 显式声明（默认值，此处写死为口径）：真实环境变量优先于 .env.test——
# G4 负控 `DB_PORT=59999 make test-e2e` 依赖该优先级，勿改成 True。
load_dotenv(ROOT / ".env.test", override=False)

# 日志文件隔离：.env.test 存在时以它的 LOG_FILE_DIR=tmp/logs 为准；被 cp 模板遗漏或
# 本地 .env.test 是旧版（无 LOG_ 组）时兜底 tmp/logs——测试期故意触发的异常不把错误
# 日志写进仓库根（tmp/ 已 gitignore）。子进程经继承拿到同值。
os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

TEST_DB = "lcode_e2e_test"
"""e2e 专用测试库（固定名；独立于 integration 的 lcode_test，互不干扰）。"""
os.environ["DB_NAME"] = TEST_DB  # 服务子进程与 alembic（本进程内跑）读同一值

# ------------------------------------------------------- e2e 开关（仅测试进程注入）
# 开关只在服务进程的装配面读取（server_entry.py 与 app 侧 lifespan 接缝），生产默认
# 行为零改动；变量清单不进 .env.example 主干（test-plan §3.1/§6-5）。
os.environ["LCODE_E2E_FAKE_LLM"] = "1"  # 进程内 ScriptedChatModel 剧本路由假模型
os.environ["LCODE_E2E_FAKE_SANDBOX"] = "1"  # 进程内 FakeSandbox（本地 tmp + 真子进程）
os.environ["LCODE_E2E_CHECKPOINTER"] = "postgres"  # 真 AsyncPostgresSaver（REQ-004 约束④）
# 假沙箱工作根：tmp/ 已 gitignore；server_entry 按 session_id 建子目录
os.environ["LCODE_E2E_SANDBOX_ROOT"] = str(ROOT / "tmp" / "e2e" / "sandboxes")

_SERVER_ENTRY = ROOT / "apps" / "server" / "test" / "e2e" / "server_entry.py"

_process: subprocess.Popen | None = None
# None = 测试库与服务可用；非 None = 不可用原因（collection 阶段据此给全部用例打 skip）
_E2E_UNAVAILABLE_REASON: str | None = None
_DB_REBUILT = False


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

    即使主进程已退出也继续清理：uvicorn 的子进程可能成为孤儿仍占端口，
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

    if _group_alive(pgid):  # 组内仍有存活成员（孤儿子进程等）→ 补刀
        _signal_group(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            _process.wait(timeout=5)

    _process = None


def _require_client(tool: str) -> None:
    """postgresql-client 前置检查：缺失即报错（明确原因，不静默 skip、不用脏库）。"""
    if shutil.which(tool) is None:
        raise RuntimeError(
            f"e2e 需要 {tool}（postgresql-client）但在 PATH 上未找到；请安装 PostgreSQL 客户端"
        )


def _admin_env(*, database: str) -> dict[str, str]:
    """管理命令的子进程环境：连接参数全走 libpq 标准变量。

    PGHOST/PGPORT/PGUSER + PGPASSWORD（密码不进 argv、不进日志）+ PGDATABASE（库名）——
    由此 dropdb/createdb 的 argv 保持全字面量，无任何拼接面。
    """
    env = dict(os.environ)
    env["PGHOST"] = os.environ["DB_HOST"]
    env["PGPORT"] = os.environ.get("DB_PORT", "5432")
    env["PGUSER"] = os.environ["DB_USER"]
    password = os.environ.get("DB_PASSWORD", "")
    if password:
        env["PGPASSWORD"] = password
    env["PGDATABASE"] = database
    return env


def _drop_test_db() -> None:
    """删 e2e 测试库（--force 断开残留连接；PG13+）。argv 参数列表、shell=False。

    dropdb/createdb 的库名是**必填位置参数**（PGDATABASE 不作缺省）——argv 只含这一个
    非字面量元素，连接参数仍走 libpq 环境变量。
    """
    _require_client("dropdb")
    result = subprocess.run(
        ["dropdb", "--if-exists", "--force", TEST_DB],
        capture_output=True,
        text=True,
        shell=False,
        env=_admin_env(database=TEST_DB),
    )
    if result.returncode != 0:
        raise RuntimeError(f"dropdb 退出码 {result.returncode}：{result.stderr.strip()}")


def _rebuild_test_db_and_migrate() -> None:
    """删/建 e2e 测试库 → alembic upgrade head（从仓库根跑，URL 复用 DB_ 配置组）。"""
    _drop_test_db()
    _require_client("createdb")
    result = subprocess.run(
        ["createdb", TEST_DB],
        capture_output=True,
        text=True,
        shell=False,
        env=_admin_env(database=TEST_DB),
    )
    if result.returncode != 0:
        raise RuntimeError(f"createdb 退出码 {result.returncode}：{result.stderr.strip()}")

    cfg = Config(str(ROOT / "apps" / "server" / "alembic.ini"))  # 服务自管迁移
    command.upgrade(cfg, "head")


def _start_service() -> None:
    """启动服务子进程并等待 /health/ready 就绪（超时 60s 报错并清理）。

    入口是 test/e2e/server_entry.py（uvicorn 直起 `app.main:app`）：进程内先按环境
    开关装配假模型/假沙箱/真 checkpointer，再开放端口——「开关只在装配面读取」的落点。
    `.env.test` 的 DB_*/SSE_*/LOG_* 已由本模块 load_dotenv 注入 os.environ，子进程继承
    （无需 --env-file）；契约包 `lcode_contracts` 由共享 venv 的 editable 安装解析，
    `test.support` 由 entry 自行把 apps/server 加入 sys.path。
    """
    global _process
    _process = subprocess.Popen(
        [
            "uv",
            "run",
            "--package",
            "lcode-server",
            "python",
            str(_SERVER_ENTRY),
            "18000",
        ],
        cwd=ROOT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _process.poll() is not None:  # 进程已退出（装配期崩溃等）→ 直接报错带退出码
            code = _process.returncode
            _stop_service()
            raise RuntimeError(f"e2e 服务启动失败：进程提前退出（exit={code}）")
        try:
            ready = httpx.get(f"{BASE_URL}/health/ready", timeout=1)
            if ready.status_code == 200 and ready.json().get("database") == "connected":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)  # 统一退避：连接失败与非 200 一律等待，避免全速空转
    _stop_service()
    raise RuntimeError("e2e 服务启动超时：/health/ready 未在 60s 内就绪")


def pytest_sessionstart(session: pytest.Session) -> None:
    """会话引导：安全闸 → 未配置 skip / 已配置则重建库+迁移+起服务，坏配置必红（G4）。

    三条出口，语义严格区分：
    - 未配置（DB_HOST/DB_USER 缺失）→ `_E2E_UNAVAILABLE_REASON`，collection 阶段整层
      显式 skip（本地没起库也能跑其它层）；
    - 配置了但坏（连接/客户端/迁移/服务启动任一失败）→ `pytest.exit(..., returncode=1)`
      终止会话——**不得转 skip**（旧实现转 skip 造成 CI 坏配置假绿，G4 负控即打此洞）；
    - 引导成功 → 正常执行（reason 恒 None，主链路用例零静默跳过）。
    """
    global _E2E_UNAVAILABLE_REASON, _DB_REBUILT

    if not TEST_DB.endswith("_test"):
        # TEST_DB 是本文件常量；此闸触发 = 代码被改坏，属缺陷而非环境缺失——红，不 skip
        pytest.exit(
            f"e2e 安全闸：测试库 {TEST_DB!r} 不以 _test 结尾，拒绝 DROP/CREATE（防误删开发库）",
            returncode=1,
        )
    if not os.environ.get("DB_HOST") or not os.environ.get("DB_USER"):
        _E2E_UNAVAILABLE_REASON = (
            "e2e 未配置：缺 DB_HOST/DB_USER（cp .env.test.example .env.test 并填连接参数）"
            "——整层显式跳过（未配置≠配置坏，后者必须红，见 G4 口径）"
        )
        return
    try:
        _rebuild_test_db_and_migrate()
        _DB_REBUILT = True  # 库已重建：此后服务起不来也要在收尾删库，不留半成品
        _start_service()
    except Exception as exc:  # noqa: BLE001 —— 连接失败/客户端缺失/迁移失败/服务起不来全转红
        _stop_service()
        pytest.exit(
            f"e2e 基础设施已配置但不可用，拒绝整层 skip（G4：坏配置必红，"
            f"DB_HOST={os.environ['DB_HOST']} DB_PORT={os.environ.get('DB_PORT', '5432')}）："
            f"{type(exc).__name__}: {exc}",
            returncode=1,
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """测试库/服务不可用时：全部 e2e 用例打显式 skip（reason 见 -ra 汇总）。

    基础设施齐备时 reason 恒 None——主链路用例一个都不允许静默跳过。
    """
    if _E2E_UNAVAILABLE_REASON is None:
        return
    skip = pytest.mark.skip(reason=_E2E_UNAVAILABLE_REASON)
    for item in items:
        item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """收尾：停服务 → 删 e2e 测试库（只清理本次真正重建过的），不留垃圾。"""
    _stop_service()
    if not _DB_REBUILT:
        return
    with contextlib.suppress(Exception):
        _drop_test_db()


@pytest.fixture
def base_url() -> str:
    """真服务基地址（e2e 测试统一经它发请求）。"""
    return BASE_URL


@pytest.fixture
async def api_client():
    """真服务 HTTP 客户端（REST + SSE 流式共用；每个测试独立连接池）。"""
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
    ) as client:
        yield client
