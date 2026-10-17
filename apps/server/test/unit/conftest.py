"""unit 层共享 fixture（本目录下所有测试模块可见）。

日志文件隔离（**必须放在任何 app 模块 import 之前**，基座铁律 11 / NFR-007）：
unit 测试读的是 `.env`（不装载 `.env.test`），`LOG_FILE_DIR` 会落到默认值 `logs/`——
而单测故意触发异常（INTERNAL/500 兜底、service_logger 带栈等），dev 分支的 ERROR
文件 sink 会把错误日志写进仓库根，污染目录。这里在 conftest 导入期（先于测试模块
收集）把目录改到 `tmp/logs`（`/tmp/` 已 gitignore）；integration/e2e 由各自 conftest
的 setdefault 与 `.env.test` 的 `LOG_FILE_DIR=tmp/logs` 覆盖。
"""

import contextlib
import os
from typing import TYPE_CHECKING

os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")  # 见模块 docstring：先于 app import

from collections.abc import Iterator  # noqa: E402 —— 见上一行的顺序要求

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

if TYPE_CHECKING:
    # loguru 的 Record 是 TypedDict，只在类型存根里（运行时不可 import）；
    # 运行期它就是普通 dict，断言按 record["level"] / record["extra"] 取键
    from loguru import Record


@pytest.fixture
def log_records() -> Iterator[list[Record]]:
    """内存捕获 sink：收集 loguru 原始 record，测试结束移除。

    三个注意点（技能基座 testing.md「loguru 日志捕获」约定）：
    - `level=0`（低于 TRACE）与 `format="{message}"`：全级别都收到，且格式串不引用
      extra（不依赖 setup_logging 装的 patcher，单测断言保持独立）；
    - 收集的是**原始 record**（`message.record`）：断言 `record["level"].name` /
      `record["extra"]` / `record["exception"]`，与输出格式串解耦；
    - teardown 按 id 移除 sink 包 suppress：`setup_logging()` 会 `logger.remove()`
      清掉全部 sink，测试若重配过日志系统，按 id 移除会找不到——属预期，不算失败。
    """
    records: list[Record] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    yield records
    with contextlib.suppress(
        ValueError
    ):  # setup_logging 已 logger.remove() 全清（sink 不存在属预期）
        logger.remove(sink_id)
