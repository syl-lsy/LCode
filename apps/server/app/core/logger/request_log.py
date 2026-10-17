"""请求日志：每个请求一条（成功走 logging 中间件 success，异常走全局处理器按级落）。"""

from dataclasses import dataclass

from app.core.logger.log_record import LogRecord


@dataclass
class RequestLog(LogRecord):
    """请求级差异字段：method / path / status_code / client_ip。

    status_code 初值 0（成功路径在中间件里补真实状态码，异常路径由处理器补），
    `_emit` 的假值过滤会让它不进 extra——只有真正拿到状态码才落盘。
    """

    method: str = ""
    path: str = ""
    status_code: int = 0
    client_ip: str = ""
