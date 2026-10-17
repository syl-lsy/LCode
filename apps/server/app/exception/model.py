"""模型域异常（A3 §8.1）。"""

from app.exception.base import BusinessException


class ModelNotFound(BusinessException):
    """会话引用了未知模型 id：会话创建/切换时比对 `config/models.yaml` 注册表（404）。"""
