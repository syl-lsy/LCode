"""ORM 层：SQLAlchemy 2.0 表模型集合（ADR-004 偏差 1：`model/` → `models/`）。

十表逐列对齐 `contracts/tables.py` 的 `TABLES` 规格（A3 §7.1，一表一文件）：

    sessions / runs / messages / tool_calls / approvals / diff_reviews /
    git_baselines / model_calls / sandbox_usage / events
    ↔ session / run / message / tool_call / approval / diff_review /
      git_baseline / model_call / sandbox_usage / event

**导入本包即聚合十表进 `Base.metadata`**（migrations/env.py 的 target_metadata 依赖
这一副作用；下方 re-export 同时也是 ruff 的「未用导入」豁免点）。
`test/unit/test_models_conformance.py` 断言 metadata ↔ TABLES 一致（CI 拦漂移）。

checkpointer 三表（checkpoints/checkpoint_writes/checkpoint_blobs）**不在此包**：
`AsyncPostgresSaver.setup()` 自管（REQ-004 约束①，A3 §7.2）。

本层是叶子：不依赖任何上层（A3 §1.2）；DTO ≠ ORM（基座铁律 2），
`schemas/` 的 Pydantic 模型与这里的表模型互不混用。
"""

from app.models.approvals import Approval
from app.models.base import Base
from app.models.diff_reviews import DiffReview
from app.models.events import Event
from app.models.git_baselines import GitBaseline
from app.models.messages import Message
from app.models.model_calls import ModelCall
from app.models.runs import Run
from app.models.sandbox_usage import SandboxUsage
from app.models.sessions import Session
from app.models.tool_calls import ToolCall

__all__ = [
    "Approval",
    "Base",
    "DiffReview",
    "Event",
    "GitBaseline",
    "Message",
    "ModelCall",
    "Run",
    "SandboxUsage",
    "Session",
    "ToolCall",
]
