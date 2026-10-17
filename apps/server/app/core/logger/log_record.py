"""日志记录基类与运行时配置（loguru）：LCode 日志体系的唯一配置处。

落点说明（A3 §1.1 / NFR-007 / 基座铁律 11）：`core/logger/` 是日志的唯一配置处——
业务代码只写领域日志（LogRecord 子类），日志类型与输出方式都由本目录决定。

- `setup_logging()` 在系统启动时调用（`main.py` 模块顶部，须在其余组件打印日志前）：
  production = stdout 单行 JSON（serialize=True，便于云日志系统解析）；development =
  彩色控制台 + ERROR 及以上额外落文件（`LOG_FILE_DIR`，默认 `logs/`，10 MB 轮转、
  保留 7 天、带异常栈）；
- `LogRecord` 是结构化日志基类：dataclass 字段即 extra，`_emit` 统一补公共字段
  （duration_ms / request_id）后按七级转发（子类见 request_log / business_log / db_log）；
- `request_id_var`：contextvars 上下文变量，由 logging 中间件按请求写入 uuid——
  该请求链路上的所有日志（业务/慢查询/异常）自动带上同一 request_id；结构上可扩展
  为 M2 的 run_id/session_id 绑定（再挂一个 contextvar 即可，不动本模块的出口面）；
- `_ensure_extras`：patcher 兜底，格式串引用了 `{extra[request_id]}` / `{extra[duration_ms]}`，
  直接 `logger.info(...)`（不经过 LogRecord）时也要保证记录不因取键失败被丢弃。
"""

import contextvars
import sys
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.core.config import common_settings, log_settings

if TYPE_CHECKING:
    # loguru 的 Record 是 TypedDict，只存在于类型存根（运行时不可 import），
    # 测试断言与 patcher 都按它取型（record["extra"] / record["level"] ...）
    from loguru import Record


class LogLevel(StrEnum):
    """loguru 支持的七个日志级别（字符串值与 loguru 的方法名一致，便于 getattr 转发）。"""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("_request_id", default="")


def _ensure_extras(record: Record) -> None:
    """patcher：给每条记录的 extra 补默认键，防格式串取键失败丢日志。

    两个 sink 的格式串都引用了 `{extra[request_id]}` 与 `{extra[duration_ms]}`——
    经 LogRecord._emit 走的日志天然带这两个键，但业务代码直接 `logger.info(...)` 时
    没有，loguru 会因格式化失败丢弃整条记录。用 setdefault 补默认值：不影响显式
    bind 的值，只兜底缺省场景。
    """
    record["extra"].setdefault("request_id", "-")
    record["extra"].setdefault("duration_ms", "-")


def setup_logging() -> None:
    """初始化日志输出（系统启动时调用一次，其余组件打印日志前完成）。

    - production：stdout 单行 JSON（serialize=True）、级别走 `LOG_LEVEL`——云日志
      采集器直接按 JSON 解析，不做多行合并；**diagnose=False**（loguru 默认 diagnose/
      backtrace 全开）——诊断信息会把出错行的局部变量值序列化进 JSON 的 text 字段，
      而这一路输出正是要发往云日志采集端的，必须在源头掐掉（NFR-004 密钥外泄面）；
    - development：stdout 彩色纯文本（人类可读）+ ERROR 及以上额外写文件
      （`LOG_FILE_DIR/errors.log`，10 MB 轮转、保留 7 天，格式串尾带 `{exception}`），
      控制台清洗后仍能回文件里查错误现场——文件 sink 保留 loguru 默认的 diagnose/
      backtrace（带局部变量现场，本地调试价值高；敏感项目按基座 logging.md §5 自行关闭）。
    """
    logger.remove()  # 清掉 loguru 默认 stderr sink：输出口径完全由本函数决定
    logger.configure(patcher=_ensure_extras)
    if common_settings.environment == "production":
        # 使用 json；diagnose=False 防局部变量值随异常现场外发（见 docstring）
        logger.add(sys.stdout, level=log_settings.level, serialize=True, diagnose=False)
    else:
        logger.add(
            sys.stdout,
            level=log_settings.level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[request_id]}</cyan> | "
                "<yellow>{extra[duration_ms]}ms</yellow> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            colorize=True,
        )
        # 路径用配置项（相对运行时 cwd，即仓库根）：不用 Path(__file__) 回溯固定层数
        # 的父目录（项目层级一变即错）；测试期由 conftest 把 LOG_FILE_DIR 指到 tmp/logs
        error_log_dir = Path(log_settings.file_dir)
        error_log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(error_log_dir / "errors.log"),
            level="ERROR",
            rotation="10 MB",
            retention="7 days",
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{extra[request_id]} | "
                "{extra[duration_ms]}ms | "
                "{name}:{function}:{line} | "
                "{message}\n{exception}"
            ),
        )


@dataclass
class LogRecord:
    """结构化日志基类：字段即结构化 extra（子类按类型追加差异字段）。

    - `message`：人类可读的日志消息，不进 extra；
    - `duration_ms`：操作耗时（毫秒）；不显式给值时由 `_emit` 用「构造到落日志」的
      实际耗时兜底（保留 2 位），如业务操作的耗时；
    - request_id 不是字段：`_emit` 从 contextvars 取当前请求的 id（无则 "-"）。
    """

    message: str
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        # 非 dataclass 字段（asdict 不会带它）：仅作 duration_ms 的兜底起点
        self._start_time: float = time.perf_counter()

    def _emit(self, level: LogLevel, exc: Exception | None = None) -> None:
        """统一落日志：过滤假值 → 补 duration_ms / request_id → 绑定 extra 按级派出。"""
        data = asdict(self)
        message = data.pop("message")

        # 过滤假值（空串 / 0 / 0.0）：结构化日志里无意义的字段不落盘
        extra = {k: v for k, v in data.items() if v}
        if not extra.get("duration_ms"):
            extra["duration_ms"] = round((time.perf_counter() - self._start_time) * 1000, 2)
        extra.setdefault("request_id", request_id_var.get() or "-")

        log = logger.bind(**extra) if extra else logger
        if exc:  # 带栈时走 opt(exception=...)，供 errors.log 的 {exception} 打印
            log = log.opt(exception=exc)
        getattr(log, level.lower())(message)

    def trace(self) -> None:
        self._emit(LogLevel.TRACE)

    def debug(self) -> None:
        self._emit(LogLevel.DEBUG)

    def info(self) -> None:
        self._emit(LogLevel.INFO)

    def success(self) -> None:
        self._emit(LogLevel.SUCCESS)

    def warning(self, exc: Exception | None = None) -> None:
        """警告级；可选 exc 用于「预期控制流里包装了基础设施故障」时附原始异常栈。"""
        self._emit(LogLevel.WARNING, exc=exc)

    def error(self, exc: Exception | None = None) -> None:
        self._emit(LogLevel.ERROR, exc=exc)

    def critical(self) -> None:
        self._emit(LogLevel.CRITICAL)
