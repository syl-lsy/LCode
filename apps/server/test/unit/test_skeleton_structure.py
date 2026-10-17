"""unit 层：骨架结构冒烟（M1 阶段 2 的目录契约，ADR-004）。

价值：M2 一开工就会往这些包里填肉，结构漂移（目录改名/包外壳丢失）应当在此时就红，
而不是等 import 失败时才发现。断言保持「不脆」——只查可导入与锚点存在，
不查内容（骨架期内容为空，M2 会变）。
"""

import importlib

import pytest
from sqlalchemy.orm import DeclarativeBase

# ADR-004 落 + 骨架包（M2 填肉）：全部必须可导入且带 docstring（骨架期的"说明书"）
SKELETON_MODULES = [
    "app.agent",
    "app.api",
    "app.api.v1",
    "app.core",
    "app.core.config",
    "app.core.db",
    "app.core.middleware",
    "app.core.openapi",
    "app.dependencies",
    "app.events",
    "app.exception",
    "app.main",
    "app.models",
    "app.models.base",
    "app.sandbox",
    "app.schemas",
    "app.schemas.internal",
    "app.services",
    "app.services.base",
    "app.utils",
]


@pytest.mark.parametrize("module_name", SKELETON_MODULES)
def test_skeleton_module_is_importable_with_docstring(module_name):
    module = importlib.import_module(module_name)
    assert module.__doc__, f"{module_name} 缺 docstring（骨架包必须写清 M2 的填充计划）"


def test_models_base_is_declarative_base():
    """ORM 基类锚点：M2 任务 1 的十表与 conformance 测试都挂在这里。"""
    from app.models.base import Base

    assert issubclass(Base, DeclarativeBase)


def test_models_package_reexports_the_same_base():
    """`app.models.Base` 与 `app.models.base.Base` 必须是同一个对象（conformance 的左侧锚点唯一）。"""
    from app.models import Base as exported
    from app.models.base import Base

    assert exported is Base
    assert Base.metadata is not None


def test_api_v1_router_prefix_matches_contract():
    """`/api/v1` 前缀与契约 rest.py 的 ENDPOINTS 一致（ADR-004 偏差 8）。"""
    from app.api.v1 import api_router

    assert api_router.prefix == "/api/v1"
    assert api_router.routes == []  # 骨架期无端点：M2 才 include_router
