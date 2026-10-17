"""数据模型规格 harness：不变量自洽 + 负例可检出。"""

import pytest

from lcode_contracts.tables import (
    TABLES,
    TABLES_BY_NAME,
    ColumnSpec,
    IndexSpec,
    TableSpec,
    check_invariants,
)

EXPECTED_TABLES = {
    "sessions", "runs", "messages", "tool_calls", "approvals",
    "diff_reviews", "git_baselines", "model_calls", "sandbox_usage", "events",
}


def test_invariants_hold() -> None:
    assert check_invariants() == []


def test_expected_table_set() -> None:
    assert {t.name for t in TABLES} == EXPECTED_TABLES


def test_all_tables_carry_user_id() -> None:
    for t in TABLES:
        uid = next((c for c in t.columns if c.name == "user_id"), None)
        assert uid is not None, f"{t.name} 缺 user_id（NFR-008）"
        assert not uid.nullable and uid.default == "'local'"


def test_negative_missing_user_id_detected() -> None:
    bad = TableSpec(
        name="bad_table",
        description="负例：缺 user_id",
        columns=[ColumnSpec.uuid_pk(), ColumnSpec.created_at()],
        indexes=[],
    )
    problems = check_invariants([bad])
    assert any("user_id" in p for p in problems)


def test_negative_dangling_fk_detected() -> None:
    bad = TableSpec(
        name="bad_fk",
        description="负例：悬空外键",
        columns=[
            ColumnSpec.uuid_pk(),
            ColumnSpec.user_id(),
            ColumnSpec.created_at(),
            ColumnSpec.fk("session_id", "nope.id"),
        ],
        indexes=[],
    )
    assert any("外键" in p for p in check_invariants([bad]))


def test_negative_events_unique_index_required() -> None:
    events = next(t for t in TABLES if t.name == "events")
    broken = events.model_copy(update={"indexes": [i for i in events.indexes if not i.unique]})
    assert any("唯一索引" in p for p in check_invariants([broken]))


def _bad_pk() -> TableSpec:
    return TableSpec(
        name="bad_pk",
        description="负例：主键非 id uuid",
        columns=[
            ColumnSpec(name="uid", type="uuid", primary_key=True, default="gen_random_uuid()"),
            ColumnSpec.user_id(),
            ColumnSpec.created_at(),
        ],
        indexes=[],
    )


def _bad_created_at() -> TableSpec:
    return TableSpec(
        name="bad_created_at",
        description="负例：created_at 类型错（非 timestamptz）",
        columns=[
            ColumnSpec.uuid_pk(),
            ColumnSpec.user_id(),
            ColumnSpec(name="created_at", type="text", default="now()"),
        ],
        indexes=[],
    )


def _bad_empty_enum() -> TableSpec:
    return TableSpec(
        name="bad_empty_enum",
        description="负例：enum_values 空列表（提供了值域却不给值）",
        columns=[
            ColumnSpec.uuid_pk(),
            ColumnSpec.user_id(),
            ColumnSpec.created_at(),
            ColumnSpec(name="status", type="varchar", enum_values=[]),
        ],
        indexes=[],
    )


def _bad_index_column() -> TableSpec:
    return TableSpec(
        name="bad_index_column",
        description="负例：索引引用不存在的列",
        columns=[ColumnSpec.uuid_pk(), ColumnSpec.user_id(), ColumnSpec.created_at()],
        indexes=[IndexSpec(name="ix_bad", columns=["nope"])],
    )


def _bad_fk_column() -> TableSpec:
    return TableSpec(
        name="bad_fk_column",
        description="负例：外键指向存在表的错误列",
        columns=[
            ColumnSpec.uuid_pk(),
            ColumnSpec.user_id(),
            ColumnSpec.created_at(),
            ColumnSpec.fk("session_id", "sessions.nope_col"),
        ],
        indexes=[],
    )


NEGATIVE_DEFECTS = [
    ("pk_not_id_uuid", [_bad_pk()], "主键"),
    ("created_at_wrong_type", [_bad_created_at()], "created_at"),
    ("empty_enum_values", [_bad_empty_enum()], "enum_values"),
    ("index_unknown_column", [_bad_index_column()], "引用不存在的列"),
    # 目标表在、列不在：走"外键目标列不存在"分支（与纯悬空表分支不同）
    ("fk_column_missing", [_bad_fk_column(), *TABLES], "外键目标列"),
]


@pytest.mark.parametrize(
    ("tables", "marker"),
    [pytest.param(t, m, id=n) for n, t, m in NEGATIVE_DEFECTS],
)
def test_invariants_detect_defects(tables: list[TableSpec], marker: str) -> None:
    """check_invariants 的每个缺陷分支都必须能检出（分支不被静默旁路）。"""
    problems = check_invariants(tables)
    assert any(marker in p for p in problems), f"未检出缺陷（期望含 {marker!r}）：{problems}"


def test_unique_indexes_for_ordering_and_rounds() -> None:
    """AC-003-4/AC-007-4 的物理承载：(session_id, seq) 与 (session_id, round) 唯一索引。"""
    expected = {
        "events": ["session_id", "seq"],
        "diff_reviews": ["session_id", "round"],
        "git_baselines": ["session_id", "round"],
    }
    for name, cols in expected.items():
        spec = TABLES_BY_NAME[name]
        assert any(i.unique and i.columns == cols for i in spec.indexes), (
            f"{name} 缺唯一索引 {cols}（顺序/轮次唯一性依赖它）"
        )


def test_approvals_auto_audit_surface() -> None:
    """AC-013-3：AUTO 决策留痕面 = approvals 行（status=auto_approved，不加列）。

    机器锚：① status 值域含 auto_approved 且与 ApprovalItem.status 对应（test_rest 侧锁定）；
    ② decided_by/decided_at/reason 列齐备（留痕三要素）；③ 表规格 description 冻结该语义。
    """
    spec = TABLES_BY_NAME["approvals"]
    status = next(c for c in spec.columns if c.name == "status")
    assert set(status.enum_values or []) == {
        "pending", "approved", "rejected", "edited", "responded", "auto_approved",
    }
    assert status.default == "'pending'"  # 人工路径默认挂起态；auto_approved 仅分类器路径写入
    cols = {c.name: c for c in spec.columns}
    for name in ("decided_by", "decided_at", "reason"):
        assert name in cols and cols[name].nullable, f"留痕列 {name} 必须可空存在"
    assert "auto_approved" in spec.description, "AUTO 留痕语义必须冻结在表规格 description"
    assert "classifier" in spec.description
