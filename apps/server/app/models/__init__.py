"""ORM 层：SQLAlchemy 2.0 表模型集合（ADR-004 偏差 1：`model/` → `models/`）。

M2 任务 1 按 `contracts/tables.py` 的 `TABLES` 规格逐列建十个模块
（`session.py / run.py / message.py / tool_call.py / approval.py / diff_review.py /
git_baseline.py / model_call.py / sandbox_usage.py / event.py`），并由
`test/unit/test_models_conformance.py` 断言 metadata ↔ TABLES 一致。

本层是叶子：不依赖任何上层（A3 §1.2）；DTO ≠ ORM（基座铁律 2），
`schemas/` 的 Pydantic 模型与这里的表模型互不混用。
"""

from app.models.base import Base

__all__ = ["Base"]
