"""事件翻译层（ADR-004 偏差 7；A3 §6）：**域事件唯一出口**。

M1 骨架只有包壳，M2 任务 7 按 A3 §6 填肉（文件级计划）：

- `translator.py`：原始流事件 → 契约域事件（AgentRef 归属推导、usage 累计、错误分类）；
- `emitter.py`：`EventPipeline`——构造载荷 → `TypeAdapter(Event)` 校验 → seq 分配 +
  INSERT + COMMIT → 广播（**先落库后广播**，重放无缝）；
- `bus.py`：`SessionEventBus`——进程内 per-session 订阅（有界队列，多订阅者）；
- `sse.py`：SSE 帧编码（`id`/`event`/`data`/`retry`/心跳）+ `StreamingResponse` 组装；
- `replay.py`：`Last-Event-ID` / `after_seq` 重放（events 表读 seq > 水位）。

两条硬约束（A3 §6.5）：
1. **不透传原始事件**（AC-008-2）：出口校验用 `lcode_contracts.events.Event` 的
   `TypeAdapter`，不在 12 型注册表内的载荷物理上发不出去；`browser_action` 属 P1
   占位，P0 由 `P0_EVENT_TYPES` 守卫拒绝。
2. **REST 来源事件的时序钉死**：业务行落库 → commit → emit（独立短事务）→ ack，
   事件永不引用未提交的业务事实（消除幽灵事件窗口）。

依赖禁令：`events/` 只允许 → `lcode_contracts`、`models/event`、`core/`；
不得 import `agent/` 或 `api/`（防循环，A3 §1.2）。
"""
