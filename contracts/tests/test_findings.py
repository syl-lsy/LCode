"""结构化输出 schema harness（Q6）。"""

import typing

import pytest
from pydantic import ValidationError

from conftest import load_fixture
from lcode_contracts.findings import Finding, ReviewerFindings

INVALID = [
    "findings/invalid/reviewer_findings__bad_severity.json",
    "findings/invalid/reviewer_findings__line_zero.json",
    "findings/invalid/reviewer_findings__end_line_without_line.json",
    "findings/invalid/reviewer_findings__end_line_before_line.json",
]


def test_valid_findings_pass() -> None:
    m = ReviewerFindings.model_validate(load_fixture("findings/valid/reviewer_findings.json"))
    assert m.schema_version == "1"
    assert m.summary  # AC-011-2：必须携带面向人的总结
    assert len(m.findings) >= 1


@pytest.mark.parametrize("rel", INVALID)
def test_invalid_findings_rejected(rel: str) -> None:
    with pytest.raises(ValidationError):
        ReviewerFindings.model_validate(load_fixture(rel))


def test_finding_direct_construction_bounds() -> None:
    with pytest.raises(ValidationError):
        Finding(file="a.py", severity="high", category="security",
                title="t", description="d", line=0)  # line 必须 >= 1
    with pytest.raises(ValidationError):
        Finding(file="a.py", severity="high", category="security",
                title="t", description="d", end_line=9)  # end_line 不得脱离 line 单独出现
    with pytest.raises(ValidationError):
        Finding(file="a.py", severity="high", category="security",
                title="t", description="d", line=48, end_line=42)  # end_line 必须 >= line


def test_finding_frozen_enum_domains() -> None:
    """Q6 定稿的枚举值域冻结：severity(5)/category(5)/confidence(3) + 默认档与 schema_version。"""
    severity = Finding.model_fields["severity"].annotation
    category = Finding.model_fields["category"].annotation
    confidence = Finding.model_fields["confidence"].annotation
    assert set(typing.get_args(severity)) == {"critical", "high", "medium", "low", "info"}
    assert set(typing.get_args(category)) == {
        "security", "correctness", "performance", "maintainability", "style",
    }
    assert set(typing.get_args(confidence)) == {"high", "medium", "low"}
    assert Finding(
        file="a.py", severity="info", category="style", title="t", description="d"
    ).confidence == "medium"

    # schema_version 唯一在册版本为 "1"：升版属契约变更，必须连带改本测试（有意为之的冻结锁）
    with pytest.raises(ValidationError):
        ReviewerFindings.model_validate({"schema_version": "2", "summary": "s"})
