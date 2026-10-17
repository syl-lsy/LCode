"""日志体系统一出口：业务代码从这里 import（`from app.core.logger import RequestLog`）。

- `LogRecord` 基类 + `RequestLog` / `BusinessLog` / `DBLog` 三个子类约定结构化字段；
- `service_logger`：业务操作日志装饰器；
- `setup_logging`：main.py 启动时调用（dev 彩色控制台 + ERROR 文件 / prod 单行 JSON）；
- `request_id_var`：logging 中间件按请求写入的上下文变量（贯穿全链路日志）。
"""

from app.core.logger.business_log import BusinessLog, service_logger
from app.core.logger.db_log import DBLog
from app.core.logger.log_record import LogRecord, request_id_var, setup_logging
from app.core.logger.request_log import RequestLog

__all__ = [
    "BusinessLog",
    "DBLog",
    "LogRecord",
    "RequestLog",
    "request_id_var",
    "service_logger",
    "setup_logging",
]
