"""可 patch 的 UTC 时钟与 uuid（test-plan-m2 §3.6）：全项目时间/ID 的唯一出口。

为什么独立成模块：unit 层要固定时间与 ID（空闲 pause 靠假时钟推进、`duration_ms`
类断言不依赖真墙钟）。测试统一 patch **本模块**的函数（`monkeypatch.setattr(
"app.utils.clock.utc_now", fake.utc_now)`），因此消费方必须写成
`from app.utils import clock` + `clock.utc_now()`（模块属性在调用时解析），
不能 `from app.utils.clock import utc_now`（那样拿到的是绑定后的旧引用，patch 无效）。

时间口径统一为 **timezone-aware UTC**（`datetime.now(UTC)`）：数据库列是 timestamptz，
裸 `datetime.now()` 会写出不带时区的值，跨环境比较必然出错。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4


def utc_now() -> datetime:
    """当前 UTC 时间（timezone-aware）。全项目取时间只走这里。"""
    return datetime.now(UTC)


def new_uuid() -> UUID:
    """新的 uuid4 对象（业务主键、事件 id 等）。"""
    return uuid4()


def new_uuid_str() -> str:
    """新的 uuid4 字符串形式（写日志 extra / 事件载荷时省一次 str()）。"""
    return str(uuid4())


def elapsed_seconds(start: datetime, end: datetime) -> int:
    """两时刻之间的整秒数（向下取整；负值归零）。

    计费分段 `duration_seconds` 与稀疏事件的时长字段统一走这里——**纯函数**，不做
    时钟读取，因此假时钟推进后传进来的两个时刻即可得到确定结果。
    """
    seconds = int((end - start).total_seconds())
    return max(seconds, 0)


__all__ = ["elapsed_seconds", "new_uuid", "new_uuid_str", "utc_now"]
