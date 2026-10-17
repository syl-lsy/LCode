"""结构化输出 schema harness（Q6）。"""

import pytest
from pydantic import ValidationError

from conftest import load_fixture
from lcode_contracts.findings import Finding, ReviewerFindings

INVALID = [
    "findings/invalid/reviewer_findings__bad_severity.json",
    "findings/invalid/reviewer_findings__line_zero.json",
    "findings/invalid/reviewer_findings__end_line_without_line.json",
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
