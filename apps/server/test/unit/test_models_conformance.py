"""unit 层：ORM metadata ↔ contracts.tables 逐表一致性（A3 §7.1 / §9.1）。

CI 拦截「模型改了、契约没动」或「迁移漏了某列/索引」这类漂移：左侧 =
`Base.metadata`（app/models 十表聚合），右侧 = `lcode_contracts.tables.TABLES_BY_NAME`
（M1 冻结的列级规格，唯一事实源）。覆盖维度（逐表、逐列）：

1. 表集合（多一少一都红；checkpointer 三表不属于业务 metadata，REQ-004 约束①）；
2. 列名集合与**顺序**（契约 `_t()` 物化顺序 = 建表 DDL 列序）；
3. 类型映射（含 PG 方言 DDL 编译产物：UUID/TEXT/VARCHAR/…/NUMERIC(12, 6)）；
4. nullable / primary_key / unique；
5. server_default 文本（SQL 字面量，与契约 default 逐字一致）；
6. 外键目标（`target_fullname` = "表.列"）；
7. enum 值域 = varchar + CHECK（名字 `ck_<表>_<列>` 稳定可映射；可空列带 IS NULL 分支；
   不建 PG 枚举类型——A3 §7.1 迁移 diff 友好）；
8. 索引名 + 列序 + unique（严格照 IndexSpec，不得改名/改列序）。

契约自身的自洽性（主键/user_id/created_at/索引列存在性）复用 `check_invariants()`，
不在本文件重写——两边口径都指向同一份契约函数。
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base
from lcode_contracts.tables import TABLES_BY_NAME, ColumnSpec, check_invariants

EXPECTED_TABLES = sorted(TABLES_BY_NAME)

# 契约 PgType → PG 方言 DDL 编译产物（本文件在「类型类」之外再断言一层真实 DDL 渲染，
# 防「语义类型对、DDL 变体错」的漂移，如 DateTime 忘了 timezone=True 时是 TIMESTAMP）
EXPECTED_DDL_TYPES: dict[str, str] = {
    "uuid": "UUID",
    "text": "TEXT",
    "varchar": "VARCHAR",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "boolean": "BOOLEAN",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "jsonb": "JSONB",
    # cost / estimated_cost 的精度口径来自 contracts.rest「落库精度 numeric(12,6)」
    "numeric": "NUMERIC(12, 6)",
}

PG_DIALECT = postgresql.dialect()


def _table(table_name: str) -> sa.Table:
    return Base.metadata.tables[table_name]


def _pg_type_of(col_type: sa.types.TypeEngine[Any]) -> str:
    """SQLAlchemy 类型 → 契约 PgType 名（判断顺序敏感：Text ⊂ String、BigInteger ⊂ Integer）。"""
    if isinstance(col_type, sa.Uuid):
        return "uuid"
    if isinstance(col_type, sa.Text):
        return "text"
    if isinstance(col_type, sa.String):
        return "varchar"
    if isinstance(col_type, sa.BigInteger):
        return "bigint"
    if isinstance(col_type, sa.Integer):
        return "integer"
    if isinstance(col_type, sa.Boolean):
        return "boolean"
    if isinstance(col_type, sa.DateTime):
        return "timestamptz"
    if isinstance(col_type, JSONB):
        return "jsonb"
    if isinstance(col_type, sa.Numeric):
        return "numeric"
    raise AssertionError(f"列类型不在契约 PgType 口径内：{col_type!r}")


def _server_default_text(column: sa.Column[Any]) -> str | None:
    """server_default 的 SQL 文本（None = 无默认值），与契约 default 字段同口径比对。"""
    default = column.server_default
    if not isinstance(default, sa.DefaultClause):  # server_default=text(...) 的固化形态
        return None
    return str(default.arg)


def _check_sql_by_name(table: sa.Table) -> dict[str, str]:
    """表级 CHECK 约束 {名字: SQL 文本}（enum 值域唯一的承载形式）。"""
    return {
        str(c.name): str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)
    }


def _expected_check_sql(col_spec: ColumnSpec) -> str:
    """契约 enum_values → 期望的 CHECK 表达式（可空列的 NULL 分支显式写出）。"""
    values = ", ".join(f"'{v}'" for v in col_spec.enum_values or [])
    expr = f"{col_spec.name} IN ({values})"
    return f"{col_spec.name} IS NULL OR {expr}" if col_spec.nullable else expr


def test_contract_specs_are_self_consistent() -> None:
    """契约自身不变量（十表、主键口径、全表 user_id/created_at、索引列存在）无问题。"""
    assert check_invariants() == []
    assert len(TABLES_BY_NAME) == 10


def test_orm_matches_contract_tables() -> None:
    """AC-004-1 锚点：metadata 表集合 = 契约十表（checkpointer 三表不得混入业务 metadata）。"""
    assert set(Base.metadata.tables) == set(TABLES_BY_NAME)


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_column_names_and_order_match_contract(table_name: str) -> None:
    """列名集合与顺序：契约 `_t()` 顺序（id, user_id, …业务列, created_at）即 DDL 列序。"""
    spec = TABLES_BY_NAME[table_name]
    assert [c.name for c in _table(table_name).columns] == [c.name for c in spec.columns]


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_column_types_match_contract(table_name: str) -> None:
    """类型映射：语义类型 + PG DDL 渲染 + timestamptz 的时区 + numeric(12,6) 精度。"""
    spec = TABLES_BY_NAME[table_name]
    table = _table(table_name)
    for col_spec in spec.columns:
        where = f"{table_name}.{col_spec.name}"
        col_type = table.columns[col_spec.name].type
        assert _pg_type_of(col_type) == col_spec.type, f"{where} 语义类型不符"
        assert col_type.compile(PG_DIALECT) == EXPECTED_DDL_TYPES[col_spec.type], (
            f"{where} DDL 渲染不符"
        )
        if col_spec.type == "timestamptz":
            assert isinstance(col_type, sa.DateTime)
            assert col_type.timezone is True, f"{where} 必须带时区（timestamptz）"
        if col_spec.type == "numeric":
            assert isinstance(col_type, sa.Numeric)
            assert (col_type.precision, col_type.scale) == (12, 6), f"{where} 精度口径"


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_column_flags_match_contract(table_name: str) -> None:
    """nullable / primary_key / unique 逐列一致（列级唯一约束只能来自契约 unique 字段）。"""
    spec = TABLES_BY_NAME[table_name]
    table = _table(table_name)
    for col_spec in spec.columns:
        where = f"{table_name}.{col_spec.name}"
        col = table.columns[col_spec.name]
        assert col.primary_key == col_spec.primary_key, f"{where} primary_key 不符"
        assert col.nullable == col_spec.nullable, f"{where} nullable 不符"
        assert bool(col.unique) == col_spec.unique, f"{where} unique 不符"


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_server_defaults_match_contract(table_name: str) -> None:
    """server_default 文本逐字一致（默认值口径在 DB 侧；Python 侧不设 client default）。"""
    spec = TABLES_BY_NAME[table_name]
    table = _table(table_name)
    for col_spec in spec.columns:
        got = _server_default_text(table.columns[col_spec.name])
        assert got == col_spec.default, f"{table_name}.{col_spec.name} server_default 不符"


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_foreign_keys_match_contract(table_name: str) -> None:
    """外键目标 = "表.列"：契约有 references 的列恰一个外键，其余列零外键。"""
    spec = TABLES_BY_NAME[table_name]
    table = _table(table_name)
    for col_spec in spec.columns:
        got = sorted(fk.target_fullname for fk in table.columns[col_spec.name].foreign_keys)
        expected = [col_spec.references] if col_spec.references else []
        assert got == expected, f"{table_name}.{col_spec.name} 外键目标不符"


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_enum_checks_match_contract(table_name: str) -> None:
    """enum 值域：恰有 `ck_<表>_<列>` CHECK 且值域/可空分支与契约 enum_values 一致。"""
    spec = TABLES_BY_NAME[table_name]
    expected = {
        f"ck_{table_name}_{col_spec.name}": _expected_check_sql(col_spec)
        for col_spec in spec.columns
        if col_spec.enum_values
    }
    assert _check_sql_by_name(_table(table_name)) == expected


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_indexes_match_contract(table_name: str) -> None:
    """索引：名字集合、列顺序、unique 三件套严格照 IndexSpec（不得改名/改列序/加索引）。"""
    spec = TABLES_BY_NAME[table_name]
    expected = {idx.name: (list(idx.columns), idx.unique) for idx in spec.indexes}
    got = {
        idx.name: ([c.name for c in idx.columns], idx.unique) for idx in _table(table_name).indexes
    }
    assert got == expected


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_every_table_has_user_id_default_local(table_name: str) -> None:
    """NFR-008：全表 user_id NOT NULL DEFAULT 'local'（P0 固定单用户；P2 前不建 users 表）。"""
    col = _table(table_name).columns["user_id"]
    assert col.nullable is False
    assert _server_default_text(col) == "'local'"


# ---------------------------------------------------------------- Python 注解口径（F8）


def test_numeric_cost_columns_are_mapped_decimal():
    """F8：Numeric(12, 6) 列的 Python 注解必须是 Mapped[Decimal]（| None）。

    SQLAlchemy `Numeric(asdecimal=True)` 默认读回即 decimal.Decimal——注解写成 float
    会误导消费方按浮点口径运算/传递（F8 的注解撒谎问题）。本测试与上面的
    DDL 编译断言互为两面：那面管数据库侧类型，这面管 Python 侧注解。
    """
    import typing
    from decimal import Decimal

    from sqlalchemy.orm import Mapped

    from app.models import ModelCall, SandboxUsage

    hints_model_call = typing.get_type_hints(ModelCall)
    hints_sandbox_usage = typing.get_type_hints(SandboxUsage)

    assert hints_model_call["cost"] == Mapped[Decimal]
    assert hints_sandbox_usage["estimated_cost"] == Mapped[Decimal | None]
