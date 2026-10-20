"""DTO 层（ADR-004 偏差 4：`schema/` → `schemas/`）：与 `models/`（ORM）严格分离。

wire DTO **不在此手写**：契约是唯一事实源（ADR-003 / NFR-002），本模块以**重导出**方式
复用契约包（单源复用，不复制字段）——API/service 只 import 本模块，校验出口因而与
contracts 冻结面保持同一份定义（`extra="forbid"` + 值域字面量即入参闸门，AC-014-1 的
「不暴露采样参数」由它机器保证）。新增消费方按需追加，命名保持契约原名。

当前重导出（M2 会话车道；其余端点随各自车道追加）：

    SessionCreate / SessionPatch / SessionDetail / SessionPage   ← lcode_contracts.rest

服务内 DTO（不出 wire 的入参/中间结构，如游标解析、事件发射入参）放
`app/schemas/internal.py`——它们不是契约的一部分，可以自由演进。
"""

from lcode_contracts.rest import SessionCreate, SessionDetail, SessionPage, SessionPatch

__all__ = [
    "SessionCreate",
    "SessionDetail",
    "SessionPage",
    "SessionPatch",
]
