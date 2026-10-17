"""REST 契约 harness：DTO fixtures 正负例 + 端点注册表自洽。"""

import typing

import pytest
from pydantic import ValidationError

from conftest import load_fixture
from lcode_contracts.errors import REST_HTTP_STATUS, RestErrorCode
from lcode_contracts.rest import (
    ENDPOINTS,
    REST_DTOS,
    ApprovalDecisionRequest,
    MessageCreate,
    ModelInfo,
    ModelList,
    SessionDetail,
    SessionPatch,
)

VALID_DTO_FIXTURES = {
    "rest/valid/message_send_ack.json": "MessageSendAck",
    "rest/valid/approval_decision_edit.json": "ApprovalDecisionRequest",
    "rest/valid/session_detail.json": "SessionDetail",
    "rest/valid/cost_summary.json": "CostSummary",
}


def test_valid_dto_fixtures_pass() -> None:
    for rel, name in VALID_DTO_FIXTURES.items():
        model = REST_DTOS[name]
        obj = model.model_validate(load_fixture(rel))
        assert obj is not None


def test_message_create_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageCreate.model_validate({"content": "   "})  # AC-001-4
    MessageCreate.model_validate({"content": " hello "})


def test_approval_decision_conditionals() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate({"decision": "respond"})  # 缺 feedback
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate({"decision": "edit"})  # 缺 edited_args
    ApprovalDecisionRequest.model_validate(
        {"decision": "respond", "feedback": "保留 build，改为清理 dist/"}
    )


def test_session_patch_requires_field() -> None:
    with pytest.raises(ValidationError):
        SessionPatch.model_validate({})
    SessionPatch.model_validate({"model": "qwen3-coder-plus"})


def test_bad_decision_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest.model_validate({"decision": "retry"})


def test_endpoint_registry_resolves() -> None:
    assert len(ENDPOINTS) >= 18
    for ep in ENDPOINTS:
        for ref in (ep.request, ep.response):
            if ref is not None:
                assert ref in REST_DTOS, f"{ep.method} {ep.path}: 未注册 DTO {ref}"
        if ep.sse:
            assert ep.method == "GET" and ep.response is None
        for code in ep.errors:
            assert code in REST_HTTP_STATUS


def test_rest_error_code_mapping_complete() -> None:
    assert set(REST_HTTP_STATUS) == set(typing.get_args(RestErrorCode))


def test_session_detail_run_state_domain() -> None:
    """run_state 三值是刷新恢复（AC-006-5）与输入区状态机的事实来源。"""
    fields = SessionDetail.model_fields["run_state"].annotation
    assert set(typing.get_args(fields)) == {"idle", "running", "waiting_approval"}


def test_model_info_effort_domain() -> None:
    m = ModelInfo.model_validate({
        "id": "qwen3-max", "display_name": "Qwen3 Max", "provider": "bailian",
        "supports_reasoning": True, "reasoning_efforts": ["low", "medium", "high"],
        "is_default": True,
    })
    assert m.reasoning_efforts  # AC-014-3：按 param_support 过滤后的可选档位
