"""LCode 后端服务包（apps/server）：按 A3 详设 `docs/arch/backend-design.md` 分层。

分层与依赖方向（单向，A3 §1.2）：

```
main.py            入口：中间件注册表 + 四处异常注册 + 双健康探针 + lifespan
core/              横切设施：config（配置唯一入口）/ db（引擎+会话工厂）/
                   middleware（注册表 + 成功信封）/ openapi（信封同步）
api/v1/            路由层：只做路由 + 参数校验 + 依赖注入（零业务逻辑）
schemas/           DTO 层（wire DTO 从 lcode_contracts.rest 重导出；服务内 DTO 放 internal.py）
services/          业务逻辑层：BaseService 范式（DTO 进出、只 flush 不 commit）
models/            ORM 层（SQLAlchemy 2.0，叶子，逐列对齐 contracts/tables.py）
agent/             编排层（外层 LangGraph 图 + 内层 deepagents 装配）
sandbox/           沙箱双适配器（bailian / opensandbox，对齐 deepagents BaseSandbox）
events/            事件翻译层（域事件唯一出口：翻译 → 校验 → 落库 → 广播 → SSE）
exception/         统一异常（领域异常 + handler/ 错误码表与全局处理器）
utils/             通用工具（clock / error_classifier）
```

禁止的依赖（review checklist）：`api/` → `models/`、`api/` → `agent|sandbox`、
`agent/` → `events/`、`events/` → `agent|api/`、`sandbox/` → `agent|services/`、
任何层硬编码连接串/密钥。
"""
