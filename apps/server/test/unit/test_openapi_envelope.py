"""unit 层：OpenAPI 信封同步（app/core/openapi.py）。

覆盖点：
- `/api/` 路径的 200 响应 $ref → `ApiResponse_<Schema>` 组件（含组件内容与 required）；
- 返回 list 的 `/api/` 路由 → `ApiResponse_List_<Schema>` 组件；
- 同名 schema 二次引用复用已有组件（不重复创建）；
- 内联 schema（$ref 数组的元素无 $ref、非数组非 $ref）就地包信封；
- 非 200/201 响应条目从 `/api/` 路径文档删除；非 `/api/` 路径保持原样；
- 非 JSON 的 /api/ 响应跳过；schema 生成后走缓存（早退分支）。

测试路由由模块级 autouse fixture 挂载、跑完移除并复位 schema 缓存，
不污染共享 app（对齐 test_exception_handlers.py 的 _remove_test_route 清理纪律）。
"""

import pytest
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import app


class ItemOut(BaseModel):
    name: str
    qty: int


class PlainOut(BaseModel):
    note: str


router = APIRouter()


@router.get("/api/_test_item", response_model=ItemOut)
async def _item() -> ItemOut:
    return ItemOut(name="n", qty=1)


@router.get("/api/_test_item_again", response_model=ItemOut)
async def _item_again() -> ItemOut:
    """同一 schema 的第二个引用：覆盖「包装组件已存在则复用」分支。"""
    return ItemOut(name="n", qty=1)


@router.get("/api/_test_items", response_model=list[ItemOut])
async def _items(q: str = "") -> list[ItemOut]:
    """带查询参数 → FastAPI 自动加 422 响应，用于验证非 200/201 条目被删除。"""
    return []


@router.get("/api/_test_ints", response_model=list[int])
async def _ints() -> list[int]:
    """数组但元素是内联 schema（非 $ref）→ 走就地包信封分支。"""
    return []


@router.get("/api/_test_inline")
async def _inline() -> dict[str, str]:
    """无 $ref 的内联对象 schema → setup 循环的 else 分支（就地包信封）。"""
    return {"k": "v"}


@router.get("/_test_plain", response_model=PlainOut)
async def _plain() -> PlainOut:
    """非 /api/ 路径：文档 schema 不得被信封包装。"""
    return PlainOut(note="x")


@router.get("/api/_test_text", response_class=PlainTextResponse)
async def _text() -> str:
    """非 JSON 的 /api/ 响应：文档侧跳过信封包装（运行时须自设 is_stream 或移出 /api/）。"""
    return "plain text"


_TEMP_PATHS = (
    "/api/_test_item",
    "/api/_test_item_again",
    "/api/_test_items",
    "/api/_test_ints",
    "/api/_test_inline",
    "/api/_test_text",
    "/_test_plain",
)


@pytest.fixture(scope="module", autouse=True)
def _temp_api_routes():
    """模块内挂载测试路由，跑完按路径移除并复位 schema 缓存，共享 app 恢复原样。"""
    app.include_router(router)
    yield
    app.router.routes = [
        r for r in app.router.routes if getattr(r, "path", None) not in _TEMP_PATHS
    ]
    # 文档缓存里还留着测试路由的包装组件，一并复位，避免影响后续用例
    app.openapi_schema = None


def _get_schema() -> dict:
    """重置缓存后重新生成 OpenAPI（避免受其他用例首次生成的缓存影响）。"""
    app.openapi_schema = None
    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 200
    return resp.json()


def test_ref_response_wrapped_as_api_envelope_component():
    schema = _get_schema()
    op = schema["paths"]["/api/_test_item"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiResponse_ItemOut"
    }

    wrapper = schema["components"]["schemas"]["ApiResponse_ItemOut"]
    assert wrapper["required"] == ["code", "data", "message"]
    assert wrapper["properties"]["data"] == {"$ref": "#/components/schemas/ItemOut"}
    assert wrapper["properties"]["code"]["type"] == "string"
    assert wrapper["properties"]["message"]["type"] == "string"


def test_second_reference_reuses_existing_wrapper_component():
    """同名 schema 的第二处引用命中「组件已存在」分支，且两处引用同一组件。"""
    schema = _get_schema()
    again = schema["paths"]["/api/_test_item_again"]["get"]
    assert again["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiResponse_ItemOut"
    }


def test_list_response_wrapped_as_list_component():
    schema = _get_schema()
    op = schema["paths"]["/api/_test_items"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiResponse_List_ItemOut"
    }

    wrapper = schema["components"]["schemas"]["ApiResponse_List_ItemOut"]
    assert wrapper["required"] == ["code", "data", "message"]
    assert wrapper["properties"]["data"]["type"] == "array"


def test_envelope_component_examples_match_contract():
    """文档侧信封与运行时信封同契约：code 示例 "0"（SUCCESS_CODE）、message 示例 "ok"。"""
    schema = _get_schema()
    properties = schema["components"]["schemas"]["ApiResponse_ItemOut"]["properties"]
    assert properties["code"]["example"] == "0"
    assert properties["message"]["example"] == "ok"
    assert properties["message"]["description"].startswith("提示信息")


def test_inline_array_items_fall_back_to_inline_envelope():
    schema = _get_schema()
    wrapped = schema["paths"]["/api/_test_ints"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    # 内联包装（不是 $ref）：data 直接是原数组 schema
    assert wrapped["properties"]["data"]["type"] == "array"
    assert wrapped["properties"]["data"]["items"] == {"type": "integer"}


def test_inline_object_schema_falls_back_to_inline_envelope():
    """非 $ref 非数组的内联对象 schema：就地包信封（data 即原 schema）。"""
    schema = _get_schema()
    wrapped = schema["paths"]["/api/_test_inline"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "$ref" not in wrapped
    assert wrapped["required"] == ["code", "data", "message"]
    assert wrapped["properties"]["data"]["type"] == "object"


def test_non_200_responses_removed_for_api_paths():
    schema = _get_schema()
    responses = schema["paths"]["/api/_test_items"]["get"]["responses"]
    assert set(responses.keys()) == {"200"}  # FastAPI 自动生成的 422 文档条目被删除


def test_non_api_path_schema_untouched():
    schema = _get_schema()
    op = schema["paths"]["/_test_plain"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PlainOut"
    }


def test_non_json_api_response_documentation_untouched():
    """非 application/json 的 /api/ 响应（文本/文件等）文档保持原样，不套信封。"""
    schema = _get_schema()
    content = schema["paths"]["/api/_test_text"]["get"]["responses"]["200"]["content"]
    assert content["text/plain"]["schema"] == {"type": "string"}
    assert "code" not in content["text/plain"]["schema"].get("properties", {})


def test_openapi_schema_is_generated_once_and_cached():
    app.openapi_schema = None
    first = app.openapi()
    assert app.openapi_schema is first
    second = app.openapi()  # 命中缓存早退分支：对象同一
    assert second is first
