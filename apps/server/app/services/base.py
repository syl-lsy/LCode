"""业务逻辑层基座：BaseService（共享会话）+ PageResult（泛型分页结果）。

用法约定（详见 references/service-layer.md）：
- 业务 service 一律继承 `BaseService`，由 api 层用依赖注入的会话构造
  （`SessionService(db).create_session(...)`），不自己建会话/引擎；
- **DTO 进 DTO 出**：service 方法入参/返回值都是 Pydantic DTO——wire DTO 直接用契约包
  （`lcode_contracts.rest`，基座铁律 10 + ADR-003），ORM 模型不出 service 层
  （`XxxDetail.model_validate(orm_obj)` 就地转换）；
- **只 flush 不 commit**：增删改在 service 内 `await self.db.flush()`（拿到自增 id、
  触发约束校验），事务提交/回滚交给请求级会话依赖 `dependencies.get_db` 统一处理；
- 请求内要做非库耗时工作（调外部 SDK / 沙箱）前，按「短事务模式」显式
  `await self.db.commit()` + `await self.db.close()` 先释放数据库连接（A3 §7.3）。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class PageResult[T]:
    """分页查询结果：service 层分页方法统一返回本类型（items 为 DTO 列表）。

    例：`async def list_sessions(self, ...) -> PageResult[SessionBrief]`。

    PEP 695 类型参数（Python 3.12+ 语法，本服务 requires-python >=3.14）：
    `PageResult[SessionBrief](items=..., total=..., page=..., page_size=...)` 用法不变。
    """

    items: list[T]
    total: int
    page: int
    page_size: int


class BaseService:
    """业务逻辑基类：所有 service 继承此类并共享同一 AsyncSession。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
