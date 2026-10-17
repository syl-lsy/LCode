"""生成物同步守门：generated/json 必须与 pydantic 单源逐字节语义一致。

这是"红→绿"交付的第一红：单源在、生成物未产出时本测试失败，提示运行
`make -C contracts gen`；生成后必须与库内产物一致（防手改/防过期）。
"""

import json

import pytest

from lcode_contracts.schema_export import GENERATED_DIR, export_all


def test_generated_json_schema_in_sync() -> None:
    if not GENERATED_DIR.exists():
        pytest.fail(
            f"{GENERATED_DIR} 不存在 —— 契约生成物尚未产出。\n"
            "红→绿流程：运行 `make -C contracts gen` 生成 generated/json（与 generated/ts），"
            "确认 diff 后提交（生成物必须入库）。"
        )
    expected = export_all()
    committed = {
        str(p.relative_to(GENERATED_DIR))[: -len(".json")]
        for p in GENERATED_DIR.rglob("*.json")
    }
    problems: list[str] = []
    for name, schema in expected.items():
        path = GENERATED_DIR / f"{name}.json"
        if not path.exists():
            problems.append(f"缺失 {name}.json")
            continue
        if json.loads(path.read_text(encoding="utf-8")) != schema:
            problems.append(f"过期 {name}.json（单源已变更，需重新生成）")
    for name in sorted(committed - set(expected)):
        problems.append(f"多余 {name}.json（导出注册表已无此项？）")
    assert not problems, (
        "generated/json 与单源不同步 —— 运行 `make -C contracts gen` 并提交：\n  "
        + "\n  ".join(problems)
    )
