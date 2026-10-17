"""数据库日志：由 core/db.py 的 SQLAlchemy 事件钩子记录（慢查询 warning）。"""

from dataclasses import dataclass

from app.core.logger.log_record import LogRecord


@dataclass
class DBLog(LogRecord):
    """数据库级差异字段：sql / params（params 只进日志，不进响应体）。"""

    sql: str = ""
    params: str = ""
