"""integration 层 conftest：装载 .env.test，DB 生命周期全套（testing.md §四 完整版）。

生命周期（session 级一次 + 每测一次，口径见 docs/arch/test-plan-m2.md §3.5）：

    sessionstart：重建测试库（DROP/CREATE <DB_NAME>）→ `alembic upgrade head` 建十表；
    每测（`db_session`）：`TRUNCATE <全表> RESTART IDENTITY CASCADE` 清脏数据；
    sessionfinish：释放连接池 → 再 DROP 测试库，不留垃圾。

安全闸（防误删开发库）：**DB_NAME 必须以 `_test` 结尾才允许破坏性语句**；否则不执行
DROP/CREATE/TRUNCATE，整层 requires_db 用例显式 skip（reason 可见）。

缺库不红（testing.md §三 约定）：DB_NAME 未配置 / 库不可达 / 迁移失败 → 记下原因，
collection 阶段给全部 requires_db 用例打显式 skip；未标记的用例照常跑——宁可显式跳过
并给出 reason，不因缺基础设施让整条命令红掉，也不静默消失。
"""

import os
from pathlib import Path

import psycopg2  # type: ignore[import-untyped]  # 无官方 stubs（替代方案：dev 组加 types-psycopg2，另案）
import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

# conftest 位于 apps/<svc>/test/integration/ 下，parents[4] 即仓库根
ROOT = Path(__file__).resolve().parents[4]
load_dotenv(ROOT / ".env.test")

# 日志文件隔离：.env.test 存在时以它的 LOG_FILE_DIR=tmp/logs 为准；被 cp 模板遗漏或
# 本地 .env.test 是旧版（无 LOG_ 组）时兜底 tmp/logs——测试期故意触发的异常不把
# 错误日志写进仓库根（tmp/ 已 gitignore）。须在 app import 之前生效。
os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

from app.core.db import get_engine  # noqa: E402 —— 必须在 load_dotenv 之后导入（settings 读 .env.test）
from app.dependencies import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

TEST_DB = os.environ.get("DB_NAME", "")

# None = 测试库可用；非 None = 不可用原因（collection 阶段据此给 requires_db 打 skip）
_REQUIRES_DB_SKIP_REASON: str | None = None
# 本次会话是否真的重建过测试库（sessionfinish 只清理自己建的东西）
_DB_REBUILT = False


def _admin_conn() -> psycopg2.extensions.connection:
    """连 postgres 管理库（不是测试库本身），用于 DROP/CREATE 测试库。"""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname="postgres",
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
    )


def _rebuild_test_db_and_migrate() -> None:
    """DROP/CREATE 测试库 → alembic upgrade head（从仓库根跑，URL 复用 DB_ 配置组）。"""
    conn = _admin_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')  # 需 PostgreSQL >= 13
        cur.execute(f'CREATE DATABASE "{TEST_DB}"')
    conn.close()

    cfg = Config(str(ROOT / "apps" / "server" / "alembic.ini"))  # 服务自管迁移
    command.upgrade(cfg, "head")


def pytest_sessionstart(session: pytest.Session) -> None:
    """整套集成测试只做一次：安全闸 → 重建测试库 → 迁移建表（失败转显式 skip）。"""
    global _REQUIRES_DB_SKIP_REASON, _DB_REBUILT

    if not TEST_DB:  # 未配置就整体跳过，sessionstart 不报错
        _REQUIRES_DB_SKIP_REASON = "未配置 DB_NAME（.env.test），requires_db 用例跳过"
        return
    if not TEST_DB.endswith("_test"):
        _REQUIRES_DB_SKIP_REASON = f"安全闸：DB_NAME={TEST_DB!r} 不以 _test 结尾，拒绝 DROP/CREATE/TRUNCATE（防误删开发库）"
        return
    try:
        _rebuild_test_db_and_migrate()
    except Exception as exc:  # 连接失败 / 权限不足 / 迁移失败都归入「测试库不可用」
        _REQUIRES_DB_SKIP_REASON = (
            f"测试库不可用（{type(exc).__name__}: {exc}）——requires_db 用例跳过"
        )
        return
    _DB_REBUILT = True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """测试库不可用时：给全部 requires_db 用例打显式 skip（reason 见 -ra 汇总）。"""
    if _REQUIRES_DB_SKIP_REASON is None:
        return
    skip = pytest.mark.skip(reason=_REQUIRES_DB_SKIP_REASON)
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """收尾 DROP 测试库（只清理本次真正重建过的），不留垃圾。"""
    if not _DB_REBUILT:
        return
    conn = _admin_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    conn.close()


@pytest.fixture
async def db_session():
    """每测清空全表（逆序 TRUNCATE + RESTART IDENTITY CASCADE），返回 AsyncSession。

    收尾 `engine.dispose()`：pytest-asyncio 默认每个测试函数一个事件循环，
    连接池里上一循环创建的 asyncpg 连接在下一循环不可用——测试结束即释放连接池，
    下一测试冷启动（integration 用例量小，代价可忽略）。
    """
    if _REQUIRES_DB_SKIP_REASON is not None:  # 兜底：未打 requires_db 的用例也不碰库
        pytest.skip(_REQUIRES_DB_SKIP_REASON)
    engine = get_engine()
    async with engine.begin() as conn:
        tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        if tables:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def approval_env(async_client, tmp_path):  # noqa: ARG001 —— 依赖它确保 app/DB 就绪
    """审批流用例的运行时三依赖替身（沙箱管理器 / checkpointer / 模型工厂）。

    ⚠️ saver 必须**每测一个实例**并在 override 里复用：依赖每请求重新求值，若现场
    构造新 saver，resume 请求拿到空 checkpoint（图从 START 重跑 + Command 输入报错）。
    事件管线走真件（真 DB 短事务 + 真 seq + 真广播）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from app.dependencies import get_checkpointer, get_model_factory, get_sandbox_manager
    from test.support.approval_env import ApprovalEnv

    env = ApprovalEnv(tmp_path)
    saver = InMemorySaver()
    app.dependency_overrides[get_sandbox_manager] = lambda: env.manager
    app.dependency_overrides[get_checkpointer] = lambda: saver
    app.dependency_overrides[get_model_factory] = lambda: env.model_factory
    yield env


@pytest.fixture
async def async_client(db_session):
    """进程内 ASGI 客户端（不开真实端口）：覆盖 get_db 依赖，指向每测清空后的 session。"""

    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
