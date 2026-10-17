"""unit 层：**契约接通**（contracts/README「阶段 2 接入挂点」的落地证据）。

contracts 是 workspace 成员，且以 **editable** 方式装进共享 venv（`[build-system]` +
`apps/server` 的 `[tool.uv.sources] lcode-contracts = { workspace = true }` 依赖边），
因此 `import lcode_contracts` 与 dev/e2e 走同一条解析路径（venv），不依赖路径环境变量。
本文件是这条链路的守门测试：

- `from lcode_contracts...` 能导入，且解析到的是仓库内源码树（不是同名第三方包）；
- 全部 canonical 合法事件 fixture 过 `TypeAdapter(Event)`（服务端唯一出口校验的等价物）；
- 非法 fixture 被**拒绝**（负例：证明校验真的在工作，而不是「什么都能过」）；
- `browser_action` 在 P0 守卫名单之外（`P0_EVENT_TYPES`，AC-008-2）；
- `ERROR_MAP` 的 code 必须是契约 `RestErrorCode`，HTTP 状态码与契约 `REST_HTTP_STATUS`
  一致（ADR-004 偏差 9 的「HTTP 映射机器可校验」）。
"""

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from app.exception.handler.errors import ERROR_MAP
from lcode_contracts.errors import REST_HTTP_STATUS, RestErrorCode
from lcode_contracts.events import EVENT_TYPES, P0_EVENT_TYPES, Event

# 本文件位于 apps/server/test/unit/ 下：parents[4] 即仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]
EVENT_FIXTURES = REPO_ROOT / "contracts" / "tests" / "fixtures" / "events"

# mypy 的 TypeForm 不支持「Annotated[Union[...], Field(discriminator=...)]」这种判别联合别名，
# 但它是 pydantic 官方推荐的校验入口（contracts/README 的 TypeAdapter(Event) 口径）→ 局部忽略
event_adapter: TypeAdapter = TypeAdapter(Event)  # type: ignore[arg-type]


def _load_fixture(relative_path: str) -> dict:
    return json.loads((EVENT_FIXTURES / relative_path).read_text(encoding="utf-8"))


def test_contracts_package_resolves_to_repo_source_tree():
    """import 到的必须是我们自己的契约源码树（排除误装同名包的假绿灯）。"""
    import lcode_contracts

    resolved = Path(lcode_contracts.__file__).resolve()
    assert resolved.is_relative_to(REPO_ROOT / "contracts" / "src")


def test_event_type_registry_is_exposed_by_contract_package():
    """12 型事件注册表可读（六族 + run_status；browser_action 为 P1 占位）。"""
    assert len(EVENT_TYPES) == 12
    assert "browser_action" in EVENT_TYPES
    assert "browser_action" not in P0_EVENT_TYPES
    assert "message_delta" in P0_EVENT_TYPES


def test_canonical_fixture_validates_through_event_adapter():
    """canonical fixture（message_delta）过 TypeAdapter —— 契约接通的直接证据。"""
    payload = _load_fixture("valid/message_delta.json")

    event = event_adapter.validate_python(payload)

    assert event.type == "message_delta"
    assert event.model_dump()["seq"] == payload["seq"]


def test_every_registered_event_type_has_validatable_canonical_fixture():
    """每个注册 type 的 canonical fixture 都能过 adapter（与 contracts 自身 harness 同口径）。"""
    for event_type in EVENT_TYPES:
        fixture = EVENT_FIXTURES / "valid" / f"{event_type}.json"
        assert fixture.exists(), f"契约缺 canonical fixture：{fixture}"
        event = event_adapter.validate_python(_load_fixture(f"valid/{event_type}.json"))
        assert event.type == event_type


def test_invalid_fixture_is_rejected():
    """负例：未知字段必须被拒（ContractModel 默认 extra="forbid"），证明校验不是空转。"""
    with pytest.raises(ValidationError):
        event_adapter.validate_python(_load_fixture("invalid/message_delta__extra_field.json"))


def test_error_map_codes_are_contract_codes_with_matching_http_status():
    """ADR-004 偏差 9：ERROR_MAP 的 code 属于契约 RestErrorCode，HTTP 与契约映射逐条一致。"""
    allowed_codes = set(get_args(RestErrorCode))

    for exc_type, (code, http_status, _message) in ERROR_MAP.items():
        assert code in allowed_codes, f"{exc_type.__name__} 的 code {code} 不在契约错误码表内"
        assert http_status == REST_HTTP_STATUS[code], (
            f"{exc_type.__name__} 的 HTTP {http_status} 与契约映射 {REST_HTTP_STATUS[code]} 不一致"
        )


def test_error_map_covers_every_contract_rest_error_code():
    """反向守门：契约错误码表里每个码都必须有落地异常（防止契约新增码而实现遗忘）。"""
    mapped = {code for code, _http, _message in ERROR_MAP.values()}
    missing = {code for code in get_args(RestErrorCode) if code not in mapped}
    assert not missing, f"契约码未映射到任何领域异常：{sorted(missing)}"
