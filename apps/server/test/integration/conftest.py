"""integration 层 conftest：装载 .env.test，提供进程内 ASGI 客户端。

骨架是「最小可运行版」（不触碰数据库）。项目接入数据库/对象存储后，按
`references/testing.md` 启用完整版：
  重建测试库 → alembic upgrade head → 每测 TRUNCATE ... RESTART IDENTITY CASCADE
  → sessionfinish 清理测试库；需真实依赖的测试用 requires_db / requires_oss 标记按需跳过。
"""

from pathlib import Path

import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

# conftest 位于 apps/<svc>/test/integration/ 下，parents[4] 即仓库根
load_dotenv(Path(__file__).resolve().parents[4] / ".env.test")

# 日志文件隔离：.env.test 存在时以它的 LOG_FILE_DIR=tmp/logs 为准；被 cp 模板遗漏或
# 本地 .env.test 是旧版（无 LOG_ 组）时兜底 tmp/logs——测试期故意触发的异常不把
# 错误日志写进仓库根（tmp/ 已 gitignore）。须在 app import 之前生效。
import os  # noqa: E402

os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

from app.main import app  # noqa: E402 —— 必须在 load_dotenv 之后导入，保证 settings 读到 .env.test


@pytest.fixture
async def async_client():
    """进程内 ASGI 客户端（不开真实端口）；接入数据库后补 dependency_overrides[get_db]。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
