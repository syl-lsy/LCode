"""LCode 契约单源包。

五域：
- events     六类领域事件族（+ run_status 运行状态族）——Q5 定稿
- rest       REST + SSE API 契约（DTO + 端点注册表）
- tables     数据模型列级规格（SQLAlchemy 2.0 + Alembic 直接可落）
- findings   结构化输出 schema（reviewer findings）——Q6 定稿
- baseline   git 基线约定（[lcode-baseline] 标签/提交规范）——REQ-007

 wire 格式统一 snake_case（决策见 ADR-003）。
"""

__version__ = "0.1.0"
