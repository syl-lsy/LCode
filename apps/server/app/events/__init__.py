"""事件翻译层（ADR-004 偏差 7；A3 §6）：**域事件唯一出口**。

五个模块各司其职（消费方按需从子模块 import，本包 re-export 常用面）：

- `translator.EventTranslator`：原始 astream 事件 → 契约域事件（AgentRef 归属推导、
  usage 累计、错误分类、todos 快照；U2 键名口径见其模块 docstring）；
- `emitter.EventPipeline`：构造载荷 → `TypeAdapter(Event)` 校验 → seq 分配（MAX+1，
  有界重试 + 抖动，唯一索引兜底）→ INSERT+COMMIT → 广播（**先落库后广播**）；
- `bus.SessionEventBus`：进程内 per-session 订阅（有界队列，多订阅者；满则断连重放）；
- `replay.iter_events_after / fetch_events_after`：events 表按 `seq > 水位` 批量重放；
- `sse`：帧编码（`id`/`event`/`data` 单行 JSON、首帧 retry、注释心跳）与
  `SSEFrameStream`（重放→实时无缝衔接，按 seq 去重；**不实现 HTTP 端点**——
  `StreamingResponse` 组装归流式接口车道）。

两条硬约束（A3 §6.5）：
1. **不透传原始事件**（AC-008-2）：出口校验用 `lcode_contracts.events.Event` 的
   `TypeAdapter`，不在 12 型注册表内的载荷物理上发不出去；`browser_action` 属 P1
   占位，P0 由 `P0_EVENT_TYPES` 守卫拒绝。
2. **REST 来源事件的时序钉死**：业务行落库 → commit → emit（独立短事务）→ ack，
   事件永不引用未提交的业务事实（消除幽灵事件窗口）。

依赖禁令（A3 §1.2）：`events/` 只允许 → `lcode_contracts`、`models/`、`core/`；
不得 import `agent/` 或 `api/`（防循环）。
"""

from app.events.bus import SessionEventBus, Subscription, SubscriptionClosed
from app.events.emitter import (
    DEFAULT_JITTER_MS,
    DEFAULT_MAX_ATTEMPTS,
    EventEmitError,
    EventPipeline,
    NonP0EventError,
    SeqAllocationError,
    ensure_p0_event,
    validate_event_payload,
)
from app.events.replay import (
    DEFAULT_BATCH_SIZE,
    fetch_events_after,
    iter_events_after,
    row_to_event,
)
from app.events.sse import (
    SSEFrameStream,
    encode_comment,
    encode_frame,
    encode_heartbeat,
    encode_retry,
    resolve_watermark,
)
from app.events.translator import (
    CUSTOM_STRUCTURED_KEY,
    DEFAULT_IDEMPOTENT_TOOLS,
    DEFAULT_RESULT_PREVIEW_LIMIT,
    DEFAULT_SUBAGENT_ROLES,
    PROTOCOL_TASK_TOOL,
    PROTOCOL_WRITE_TODOS_TOOL,
    ApprovalTarget,
    EventTranslator,
    InFlightToolCall,
    InterruptMappingError,
    ModelCallRecord,
)

__all__ = [
    "CUSTOM_STRUCTURED_KEY",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_IDEMPOTENT_TOOLS",
    "DEFAULT_JITTER_MS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RESULT_PREVIEW_LIMIT",
    "DEFAULT_SUBAGENT_ROLES",
    "ApprovalTarget",
    "EventEmitError",
    "EventPipeline",
    "EventTranslator",
    "InFlightToolCall",
    "InterruptMappingError",
    "ModelCallRecord",
    "NonP0EventError",
    "PROTOCOL_TASK_TOOL",
    "PROTOCOL_WRITE_TODOS_TOOL",
    "SSEFrameStream",
    "SeqAllocationError",
    "SessionEventBus",
    "Subscription",
    "SubscriptionClosed",
    "encode_comment",
    "encode_frame",
    "encode_heartbeat",
    "encode_retry",
    "ensure_p0_event",
    "fetch_events_after",
    "iter_events_after",
    "resolve_watermark",
    "row_to_event",
    "validate_event_payload",
]
