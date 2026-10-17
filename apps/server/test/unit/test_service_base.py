"""unit 层：service 层基座（app/services/base.py）。

覆盖点：PageResult 的四个字段与泛型行为、BaseService 持有注入的会话对象。
"""

from dataclasses import is_dataclass
from unittest.mock import MagicMock

from app.services.base import BaseService, PageResult


def test_page_result_carries_pagination_fields():
    result = PageResult[int](items=[1, 2, 3], total=3, page=2, page_size=10)
    assert result.items == [1, 2, 3]
    assert result.total == 3
    assert result.page == 2
    assert result.page_size == 10


def test_page_result_is_dataclass_and_generic():
    assert is_dataclass(PageResult)
    # 泛型参数只影响类型标注，运行时原样持有 items
    result = PageResult[str](items=["a"], total=1, page=1, page_size=20)
    assert result.items == ["a"]


def test_base_service_holds_injected_session():
    session = MagicMock()
    service = BaseService(db=session)
    assert service.db is session
