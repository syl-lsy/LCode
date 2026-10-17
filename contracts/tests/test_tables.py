"""数据模型规格 harness：不变量自洽 + 负例可检出。"""

from lcode_contracts.tables import TABLES, ColumnSpec, TableSpec, check_invariants

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
