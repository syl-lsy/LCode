"""enum 值域的 CHECK 约束工厂（十表共用；A3 §7.1：varchar + CHECK，不建 PG 枚举类型）。

约束名固定 `ck_<表>_<列>`——本仓库刻意不设 naming_convention（base.py 的既定决策），
约束名必须显式给全且稳定，conformance 测试按该命名映射回契约 enum_values 逐值断言。

可空列显式补 `IS NULL OR` 分支：SQL 三值逻辑下裸 `CHECK (col IN (...))` 遇 NULL 本即
放行，写明是为了让 DDL 自解释（审查时不必脑补 NULL 语义），也让「可空枚举列」与
「非空枚举列」在 pg_dump / psql 描述输出里一眼可辨。
"""

from collections.abc import Sequence

from sqlalchemy import CheckConstraint


def enum_check(
    table: str, column: str, values: Sequence[str], *, nullable: bool = False
) -> CheckConstraint:
    """构造 `<column> IN (值域)` 的表级 CHECK；nullable 列附 `column IS NULL OR …` 分支。"""
    allowed = ", ".join(f"'{value}'" for value in values)
    expr = f"{column} IN ({allowed})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"ck_{table}_{column}")
