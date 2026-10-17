"""ORM 声明基类：所有表模型继承 `Base`（M2 任务 1 起逐表落地）。

刻意**不设置命名约定（naming_convention）**：`contracts/tables.py` 的 `IndexSpec`
已给出逐表显式索引名（`ix_sessions_user_active` 等），metadata ↔ TABLES 的
conformance 测试按名字逐项断言——若在此加自动命名规则，索引名会被改写而与契约
对不上（契约冻结，索引名同样属于 wire 之外的规格）。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """ORM 基类：`Base.metadata` 是全部表规格的聚合点（conformance 测试的左侧锚点）。"""


__all__ = ["Base"]
