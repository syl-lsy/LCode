"""事件协议 harness（Q5 冻结面的守门测试）。

- 合法 fixtures 必须通过带判别器的 Event 联合校验；
- 非法 fixtures（缺必填/类型错/枚举外/未知字段/未知 type/跨字段矛盾）必须被拒；
- 注册表与 fixtures 一一对应（新增事件类型没有合法样例 = 红）。
"""

import json
import typing
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from conftest import FIXTURES, load_fixture
from lcode_contracts import events as events_mod
from lcode_contracts.events import EVENT_TYPES, P0_EVENT_TYPES, Event

ADAPTER = TypeAdapter(Event)
VALID_DIR = FIXTURES / "events" / "valid"
INVALID_DIR = FIXTURES / "events" / "invalid"


@pytest.mark.parametrize("path", sorted(VALID_DIR.glob("*.json")), ids=lambda p: p.name)
def test_valid_events_pass(path: Path) -> None:
    evt = ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    assert evt.type in EVENT_TYPES


@pytest.mark.parametrize("path", sorted(INVALID_DIR.glob("*.json")), ids=lambda p: p.name)
def test_invalid_events_rejected(path: Path) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def test_registry_fixture_coverage() -> None:
    """每个注册的 type 必须有恰好一个 canonical 合法 fixture：{type}.json。"""
    missing = [t for t in EVENT_TYPES if not (VALID_DIR / f"{t}.json").exists()]
    assert not missing, f"事件类型缺 canonical fixture：{missing}"


def test_valid_fixture_type_matches_filename() -> None:
    for path in VALID_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["type"] in EVENT_TYPES, f"{path.name}: 未注册的 type {data['type']}"
        assert path.stem == data["type"] or path.stem.startswith(data["type"] + "_"), (
            f"{path.name}: 文件名与 type {data['type']} 不对应"
        )


def test_p0_event_registry() -> None:
    """P0 可产出集合 = 六类事件族细分 + run_status；browser_action 为 P1 占位。"""
    assert P0_EVENT_TYPES == {
        "message_created", "message_delta", "message_finished",
        "tool_call_started", "tool_call_finished",
        "approval_request", "approval_resolved",
        "diff_ready", "diff_resolved",
        "sandbox_output",
        "run_status",
    }
    assert "browser_action" in EVENT_TYPES and "browser_action" not in P0_EVENT_TYPES


def test_registry_union_alignment_no_duplicate_types() -> None:
    """注册表完整性：类集合 ↔ type 字面量 ↔ 判别器联合三方对齐，且无重复字面量。

    重复 type 会让 EVENT_TYPES 静默遮蔽一个类；联合与类集合脱节会让
    "注册了但校验不到 / 校验到但没注册" 的漂移潜伏到出口才发现。
    """
    classes = events_mod._EVENT_CLASSES
    literals = [c.model_fields["type"].default for c in classes]
    assert len(set(literals)) == len(literals), f"重复 type 字面量：{literals}"
    assert set(EVENT_TYPES) == set(literals)
    union_classes = set(typing.get_args(typing.get_args(events_mod.Event)[0]))
    assert union_classes == set(classes), "判别器联合与注册类集合不一致（新增事件类必须进 _EVENT_CLASSES）"


def test_seq_boundary_and_duration_bound() -> None:
    """数值下界约束（ge=0）：seq=0 是合法下边界；负 seq / 负 duration_ms 必须被拒。"""
    delta = load_fixture("events/valid/message_delta.json")
    delta["seq"] = 0
    assert ADAPTER.validate_python(delta).seq == 0

    finished = load_fixture("events/valid/tool_call_finished.json")
    finished["duration_ms"] = -1
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(finished)


def test_negative_coverage_evidence() -> None:
    """负例必须覆盖的证据类别：缺必填 / 类型错 / 枚举外 / 未知字段 / 未知 type。"""
    names = " ".join(p.name for p in INVALID_DIR.glob("*.json"))
    for marker in ("missing", "seq_string", "enum", "extra_field", "unknown"):
        assert marker in names, f"负例缺少证据类别：{marker}"
