"""OpenAPI 信封同步：把 `/api/` 路径的成功响应 schema 包装成统一信封。

`/api/` 路径的成功响应在运行时被 `middleware/response.py` 包装为契约信封
`{"code": "0", "data": <DTO>, "message": "ok"}`（`contracts/rest.py` 模块 docstring），
若文档不同步，/docs 与生成的 SDK 都会拿到错误契约。
本模块 monkey-patch `app.openapi`，在文档生成后：

- 仅处理 `/api/` 前缀路径，只保留 200/201 响应（错误格式由 `generate_error_docs()`
  在 API 描述里统一说明，逐条错误 schema 从文档删除）；
- `$ref` 响应 → 复用/新建 `ApiResponse_<Schema>` 组件；数组响应（items 为 `$ref`）
  → 复用/新建 `ApiResponse_List_<Schema>` 组件；其余内联 schema 直接就地包一层信封。

成功与错误两侧是同一份响应契约：改信封格式必须同时改本文件与
`middleware/response.py`，两侧一起动。
"""

from typing import Any

from fastapi import FastAPI


def _build_envelope_properties(data_schema: dict[str, Any]) -> dict[str, Any]:
    """信封三字段的 OpenAPI 描述（data 挂业务 schema）。"""
    return {
        "code": {
            "type": "string",
            "description": '状态码，契约 SUCCESS_CODE："0" 表示成功',
            "example": "0",
        },
        "data": data_schema,
        "message": {
            "type": "string",
            "description": '提示信息（成功恒为 "ok"）',
            "example": "ok",
        },
    }


def _make_envelope(data_schema: dict[str, Any]) -> dict[str, Any]:
    """就地包装内联 schema（非 $ref、非 $ref 数组的响应体）。"""
    return {
        "type": "object",
        "properties": _build_envelope_properties(data_schema),
        "required": ["code", "data", "message"],
    }


def _wrap_ref_schema(openapi_schema: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """`$ref` 响应 → `ApiResponse_<Schema>` 组件（已存在则复用）。"""
    ref_path: str = schema["$ref"]
    ref_name = ref_path.split("/")[-1]

    title = ref_name.replace("_", " ")
    wrapper_name = f"ApiResponse_{ref_name}"

    schemas = openapi_schema.setdefault("components", {}).setdefault("schemas", {})
    if wrapper_name not in schemas:
        schemas[wrapper_name] = {
            "title": f"ApiResponse[{title}]",
            "type": "object",
            "properties": _build_envelope_properties(schema),
            "required": ["code", "data", "message"],
        }

    return {"$ref": f"#/components/schemas/{wrapper_name}"}


def _wrap_array_schema(openapi_schema: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """数组响应 → items 是 `$ref` 时建 `ApiResponse_List_<Schema>` 组件，否则内联包装。"""
    items = schema.get("items")
    if isinstance(items, dict) and "$ref" in items:
        item_name = items["$ref"].split("/")[-1]
        title = f"List[{item_name.replace('_', ' ')}]"
        wrapper_name = f"ApiResponse_List_{item_name}"

        schemas = openapi_schema.setdefault("components", {}).setdefault("schemas", {})
        if wrapper_name not in schemas:
            schemas[wrapper_name] = {
                "title": f"ApiResponse[{title}]",
                "type": "object",
                "properties": _build_envelope_properties(schema),
                "required": ["code", "data", "message"],
            }

        return {"$ref": f"#/components/schemas/{wrapper_name}"}

    return _make_envelope(schema)


def setup_openapi(app: FastAPI) -> None:
    """挂载信封同步：在文档首次生成前调用即可。

    openapi 生成是惰性的——patch 后真正生成时才读取当时已注册的全部路由，
    因此调用点放在 include_router 之前或之后都成立（本项目在 main.py 顶部调用）。
    """
    _original = app.openapi

    def _custom() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = _original()

        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/api/"):
                continue

            for method in ("get", "post", "put", "delete", "patch"):
                operation = path_item.get(method)
                if operation is None:
                    continue

                responses = operation.get("responses", {})
                for status_code_str, response in list(responses.items()):
                    # 非数字键（如手写 `responses={"default": ...}`）不是 200/201 → 与
                    # 其他非成功响应一致地删除；不能直接 int()（ValueError 会让整个
                    # /openapi.json 生成 500）
                    if not status_code_str.isdigit():
                        del responses[status_code_str]
                        continue

                    status_code = int(status_code_str)
                    if status_code not in (200, 201):
                        del responses[status_code_str]
                        continue

                    content = response.get("content", {})
                    json_content = content.get("application/json")
                    if json_content is None:
                        continue

                    original_schema = json_content.get("schema")
                    if original_schema is None:
                        continue

                    if "$ref" in original_schema:
                        wrapped = _wrap_ref_schema(schema, original_schema)
                    elif original_schema.get("type") == "array":
                        wrapped = _wrap_array_schema(schema, original_schema)
                    else:
                        wrapped = _make_envelope(original_schema)

                    json_content["schema"] = wrapped

        app.openapi_schema = schema
        return schema

    app.openapi = _custom  # type: ignore[method-assign]
