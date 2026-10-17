"""异常处理器包：统一处理器 + 错误码表 + 文档生成，由 `main.py` 接线。"""

from app.exception.handler.docs import generate_error_docs
from app.exception.handler.handlers import exception_handler

__all__ = ["exception_handler", "generate_error_docs"]
