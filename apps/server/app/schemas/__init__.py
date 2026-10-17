"""DTO 层（ADR-004 偏差 4：`schema/` → `schemas/`）：与 `models/`（ORM）严格分离。

wire DTO **不在此手写**：契约是唯一事实源（ADR-003 / NFR-002），M2 起本模块以
重导出方式复用契约包（单源复用，不复制字段）：

```python
from lcode_contracts.rest import SessionCreate, SessionDetail, ...  # noqa: F401
```

服务内 DTO（不出 wire 的入参/中间结构，如游标解析、事件发射入参）放
`app/schemas/internal.py`——它们不是契约的一部分，可以自由演进。
"""
