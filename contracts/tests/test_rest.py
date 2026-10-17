"""REST 契约 harness：DTO fixtures 正负例 + 端点注册表自洽。"""

import typing

import pytest
from pydantic import ValidationError

from conftest import FIXTURES, load_fixture
from lcode_contracts.errors import REST_HTTP_STATUS, SUCCESS_CODE, RestErrorCode
from lcode_contracts.rest import (
    ENDPOINTS,
    REST_DTOS,
    ApprovalDecisionRequest,
    CostSummary,
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

INVALID_DTO_FIXTURES = {
    "rest/invalid/message_create__blank.json": "MessageCreate",
    "rest/invalid/session_patch__empty.json": "SessionPatch",
    "rest/invalid/approval_decision__bad_decision.json": "ApprovalDecisionRequest",
    "rest/invalid/approval_decision__respond_without_feedback.json": "ApprovalDecisionRequest",
}


def test_valid_dto_fixtures_pass() -> None:
    for rel, name in VALID_DTO_FIXTURES.items():
        model = REST_DTOS[name]
        obj = model.model_validate(load_fixture(rel))
        assert obj is not None


@pytest.mark.parametrize(
    ("rel", "name"),
    sorted(INVALID_DTO_FIXTURES.items()),
    ids=[r.rsplit("/", 1)[-1] for r in sorted(INVALID_DTO_FIXTURES)],
)
def test_invalid_dto_fixtures_rejected(rel: str, name: str) -> None:
    with pytest.raises(ValidationError):
        REST_DTOS[name].model_validate(load_fixture(rel))


def test_rest_fixture_wiring_complete() -> None:
    """rest/ 下每个 fixture 必须被映射表消费（防"死文件"：写了负例但没接线）。"""
    for sub, table in (("valid", VALID_DTO_FIXTURES), ("invalid", INVALID_DTO_FIXTURES)):
        on_disk = {f"rest/{sub}/{p.name}" for p in (FIXTURES / "rest" / sub).glob("*.json")}
        assert on_disk == set(table), f"rest/{sub} fixtures 与映射表不一致：{on_disk ^ set(table)}"


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


def test_endpoint_registry_invariants() -> None:
    """注册表结构不变量：路由唯一、统一 /api/v1/ 前缀（信封适用范围）、无响应体仅 204/SSE。"""
    keys = [(ep.method, ep.path) for ep in ENDPOINTS]
    assert len(set(keys)) == len(keys), "method+path 重复（后注册会遮蔽先注册）"
    for ep in ENDPOINTS:
        assert ep.path.startswith("/api/v1/"), f"{ep.path}: 前缀外路径不受成功信封约束"
        if ep.response is None:
            assert ep.method == "DELETE" or ep.sse, (
                f"{ep.method} {ep.path}: 无响应体只允许 DELETE(204) 或 SSE(裸事件流)"
            )


def test_success_code_constant() -> None:
    """成功信封 code 恒为字符串 "0"（contracts/README §六）。"""
    assert SUCCESS_CODE == "0"


def test_rest_error_code_mapping_complete() -> None:
    assert set(REST_HTTP_STATUS) == set(typing.get_args(RestErrorCode))


def test_cost_summary_dimension_domain() -> None:
    """AC-015-2：成本汇总固定会话/日/模型三维，不接受第四维。"""
    fields = CostSummary.model_fields["dimension"].annotation
    assert set(typing.get_args(fields)) == {"session", "date", "model"}


def test_sampling_params_never_in_contract() -> None:
    """AC-014-1 / 范围外清单（永久）：采样参数不得进入任何 wire 契约（extra=forbid 硬拒）。"""
    with pytest.raises(ValidationError):
        ModelInfo.model_validate({
            "id": "qwen3-max", "display_name": "Qwen3 Max", "provider": "bailian",
            "supports_reasoning": True, "reasoning_efforts": ["low", "medium", "high"],
            "temperature": 0.7,
        })
    with pytest.raises(ValidationError):
        SessionPatch.model_validate({"temperature": 0.7})


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
