"""服务内 DTO（不出 wire）：契约之外的内部数据结构。

与 `app/schemas/__init__.py` 的分界：**凡是客户端可见的字段都归契约包**
（`lcode_contracts.rest` / `.events`），这里只放服务内部传参/中间结构。

- `Cursor` / `encode_cursor()` / `decode_cursor()`：游标分页的水位编解码
  （messages / approvals 列表共用；契约只把游标定义为**不透明字符串**
  `next_cursor`，结构属服务端内部约定，改结构不需契约变更）；
- `EmitContext`：事件发射入参（session_id / run_id / agent 归属）——由
  `events/emitter.py` 消费，避免 service 层直接拼事件信封（M2 任务 7 事件车道落地）；
- `SessionModelCfg`：run 启动时从会话行读出的模型/档位快照（get_llm 的会话级入参；
  注册表已有同构的 `agent.models_registry.SessionModelCfg`，届时按需复用或再导出）。
"""

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.exception.common import InvalidArgument

_CURSOR_DETAIL = "游标非法：请原样回传上一页响应的 next_cursor"


@dataclass(frozen=True, slots=True)
class Cursor:
    """游标水位：键集分页排序键 `(created_at, id)` 上「已发出的最后一行」快照。

    键集分页（keyset pagination）用行值比较替代 offset：下一页查询 `(created_at, id) <
    水位` 且排序方向一致（契约 `next_cursor` 为空表示尾页），深翻页不退化、不重不漏。
    `id` 为 UUID 字符串（wire 层 id 口径），是**第二排序键**——同一 created_at 的行靠它定序。
    """

    created_at: datetime
    id: str


def encode_cursor(cursor: Cursor) -> str:
    """水位 → 不透明游标字符串（base64url(JSON)，可直接进 query string）。"""
    payload = json.dumps(
        {"created_at": cursor.created_at.isoformat(), "id": cursor.id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> Cursor:
    """游标字符串 → 水位；任何非法输入 → `InvalidArgument`（契约 INVALID_ARGUMENT 422）。

    非法集合：非 base64url 字母表 / JSON 解析失败 / 载荷非对象 / 缺字段或类型不符 /
    `created_at` 不可解析 / `id` 非 UUID。缺时区的 `created_at` 按 UTC 补齐——naive 值
    不能与 `timestamptz` 列比较（asyncpg 会直接报错，外部输入不该把库层错误引进来）。
    """
    try:
        raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:  # binascii.Error 是 ValueError 子类
        raise InvalidArgument(
            _CURSOR_DETAIL, detail="非 base64url 编码", original_exception=exc
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidArgument(
            _CURSOR_DETAIL, detail="载荷不是合法 JSON", original_exception=exc
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidArgument(_CURSOR_DETAIL, detail=f"载荷不是对象：{type(payload).__name__}")

    created_at_raw = payload.get("created_at")
    identity_raw = payload.get("id")
    if not isinstance(created_at_raw, str) or not isinstance(identity_raw, str):
        raise InvalidArgument(_CURSOR_DETAIL, detail="缺 created_at/id 字段（或类型非字符串）")

    try:
        created_at = datetime.fromisoformat(created_at_raw)
    except ValueError as exc:
        raise InvalidArgument(
            _CURSOR_DETAIL, detail="created_at 不可解析", original_exception=exc
        ) from exc
    try:
        uuid.UUID(identity_raw)
    except ValueError as exc:
        raise InvalidArgument(
            _CURSOR_DETAIL, detail="id 不是 UUID", original_exception=exc
        ) from exc

    if created_at.tzinfo is None:  # naive 值不能与 timestamptz 列比较
        created_at = created_at.replace(tzinfo=UTC)
    return Cursor(created_at=created_at, id=identity_raw)
