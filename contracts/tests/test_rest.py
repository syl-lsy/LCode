"""REST 契约 harness：DTO fixtures 正负例 + 端点注册表自洽。"""

import typing

import pytest
from pydantic import ValidationError

from conftest import FIXTURES, load_fixture
from lcode_contracts.errors import REST_HTTP_STATUS, SUCCESS_CODE, RestErrorCode
from lcode_contracts.events import SandboxOutputEvent
from lcode_contracts.rest import (
    ENDPOINTS,
    REST_DTOS,
    ApprovalDecisionRequest,
    ApprovalItem,
    CostSummary,
    MessageCreate,
    ModelInfo,
    ModelList,
    QueryParamSpec,
    SandboxStatus,
    SessionDetail,
    SessionPatch,
)
from lcode_contracts.tables import TABLES_BY_NAME

VALID_DTO_FIXTURES = {
    "rest/valid/message_send_ack.json": "MessageSendAck",
    "rest/valid/approval_decision_edit.json": "ApprovalDecisionRequest",
    "rest/valid/session_detail.json": "SessionDetail",
    "rest/valid/cost_summary.json": "CostSummary",
    "rest/valid/approval_item_auto.json": "ApprovalItem",
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
    assert len(ENDPOINTS) == 19  # 端点总数为契约事实（README/详设计数同步）
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


# ---------------------------------------------------------------- 查询参数规格（P1-3）

EXPECTED_QUERY_PARAMS = {
    ("GET", "/api/v1/sessions"): {"title"},
    ("GET", "/api/v1/sessions/{session_id}/messages"): {"cursor"},
    ("GET", "/api/v1/sessions/{session_id}/events"): {"after_seq"},
    ("GET", "/api/v1/approvals"): {"status", "session_id"},
    ("GET", "/api/v1/costs/summary"): {"dimension"},
}


def test_query_param_names_locked() -> None:
    """查询参数名集合锁定（前端 URL 构造与后端解析的唯一契约面，变更走增量 diff）。"""
    actual = {
        (ep.method, ep.path): {q.name for q in ep.query}
        for ep in ENDPOINTS
        if ep.query
    }
    assert actual == EXPECTED_QUERY_PARAMS


def test_query_param_specs_well_formed() -> None:
    for ep in ENDPOINTS:
        names = [q.name for q in ep.query]
        assert len(set(names)) == len(names), f"{ep.method} {ep.path}: 查询参数重名"
        for q in ep.query:
            assert q.name and q.name.replace("_", "").isalnum(), f"{ep.path}: 参数名非 wire 形态"
            if q.type == "enum":
                assert q.enum, f"{ep.path}?{q.name}: enum 型必须给出非空值域"
            else:
                assert q.enum is None, f"{ep.path}?{q.name}: enum 值域仅限 enum 型"


def test_query_param_enum_domains() -> None:
    """dimension 三维（AC-015-2）；approvals status 值域 = ApprovalItem.status 逐值一致。"""
    by_key = {(ep.method, ep.path): ep for ep in ENDPOINTS}
    dim = next(q for q in by_key[("GET", "/api/v1/costs/summary")].query if q.name == "dimension")
    assert dim.required and set(dim.enum or []) == {"session", "date", "model"}
    st = next(q for q in by_key[("GET", "/api/v1/approvals")].query if q.name == "status")
    dto_domain = set(typing.get_args(ApprovalItem.model_fields["status"].annotation))
    assert set(st.enum or []) == dto_domain


def test_query_param_enum_requires_values() -> None:
    with pytest.raises(ValidationError):
        QueryParamSpec(name="status", type="enum")  # 缺值域
    with pytest.raises(ValidationError):
        QueryParamSpec(name="q", type="string", enum=["x"])  # 非 enum 型带值域


def test_diff_file_endpoint_path_converter_noted() -> None:
    """file_path 含 `/`：实现必须用 :path 路由转换器（summary 锁定该实现注意）。"""
    ep = next(e for e in ENDPOINTS if e.path.endswith("/files/{file_path}"))
    assert ":path" in ep.summary


# ---------------------------------------------------------------- AUTO 审计面（P1-2 / AC-013-3）

def test_approval_item_status_matches_table_enum() -> None:
    """ApprovalItem.status 与 approvals 表 status 值域逐值一致——AUTO 审计行必须可投影。"""
    dto_domain = set(typing.get_args(ApprovalItem.model_fields["status"].annotation))
    status_col = next(c for c in TABLES_BY_NAME["approvals"].columns if c.name == "status")
    assert dto_domain == set(status_col.enum_values or [])
    assert "auto_approved" in dto_domain  # AC-013-3：分类器决策留痕的投影值
    # 审计承载列：decided_by/decided_at/reason（表规格 description 冻结语义的机器锚）
    col_names = {c.name for c in TABLES_BY_NAME["approvals"].columns}
    assert {"decided_by", "decided_at", "reason"} <= col_names


def _literal_strings(annotation: typing.Any) -> set[str]:
    """展开 Literal（含 Optional 包裹）内的字符串值域。"""
    out: set[str] = set()
    for arg in typing.get_args(annotation):
        nested = typing.get_args(arg)
        if nested:
            out |= {v for v in nested if isinstance(v, str)}
        elif isinstance(arg, str):
            out.add(arg)
    return out


def test_sandbox_status_phase_is_state_projection() -> None:
    """查询面 phase = 稳态投影；事件面含迁移动作（resumed/recreated）——差异为有意设计，映射写进 description。"""
    state = _literal_strings(SandboxStatus.model_fields["phase"].annotation)
    event = _literal_strings(SandboxOutputEvent.model_fields["phase"].annotation)
    assert event - state == {"resumed", "recreated"}, "事件侧迁移动作必须折叠出状态面"
    assert state - event == {"none"}, "none 仅在状态面（未有沙箱实例）"
    desc = SandboxStatus.model_fields["phase"].description or ""
    assert "resumed" in desc and "running" in desc, "映射关系必须写在 Field description"
