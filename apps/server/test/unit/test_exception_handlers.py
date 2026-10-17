"""unit 层：统一异常处理器（app/exception/handler/）全分支覆盖。

覆盖点：
- `_lookup`：MRO 命中已注册类型 / 未注册子类回退 / 非 Exception 防御分支；
- `exception_handler` 直 await：协议错误（ERROR_MAP 与 PROTOCOL_ERROR_MAP 两路，
  含 detail 不可用时的归一）、领域异常（**契约字符串码**）、校验错误、未知异常兜底
  （哨兵断言敏感信息不泄漏）；
- 日志承诺（**每请求恰一条的异常侧出口**，基座铁律 11）：有 request_log 时走 loguru
  单管线——先补 status_code 再按级落（协议/校验/业务 warning，未知异常 error 带栈），
  业务异常 detail 进 message、仅 original_exception 存在时带栈；request_log 缺失
  （不在中间件链内直调处理器）时回退标准库 logger，severity/message 口径一致；
- TestClient 走真实注册链路：`/nonexistent` 404、非法参数 INVALID_ARGUMENT、
  未捕获异常 INTERNAL、BusinessException 在默认 TestClient 下内联返回统一 JSON（不冒泡）；
  **404/422/500 各恰好一条对应级别日志、都没有 SUCCESS**（请求日志由中间件/处理器二选一）；
- `generate_error_docs()` 与 `app.description`（错误码表自动进文档）；
- 防漂移：`app.exception` 导出的领域异常必须已在 ERROR_MAP 注册。

直调用例的 request 用最小 stub（`SimpleNamespace(state=SimpleNamespace(request_log=...))`，
处理器只读 `request.state.request_log` 并写 `exception_handled` 标志）——**不要用
`MagicMock()`**：它的任意属性都 truthy，`getattr(request.state, "request_log", None)`
会返回自动生成的 mock（非 None），误走 loguru 分支、把标准库兜底路径的 caplog 断言
打空。loguru 捕获 fixture（log_records）见 test/unit/conftest.py。
"""

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from starlette.responses import Response

import app.exception as exception_pkg
from app.core.logger import RequestLog
from app.exception import BusinessException, MessageEmpty, SessionNotFound
from app.exception.handler import exception_handler, generate_error_docs
from app.exception.handler.errors import ERROR_MAP
from app.exception.handler.handlers import _lookup
from app.main import app

if TYPE_CHECKING:
    from fastapi import Request

    # loguru 的 Record 是 TypedDict，只在类型存根里（运行时不可 import）
    from loguru import Record

HANDLER_LOGGER = "app.exception.handler.handlers"


def _request_with_log(request_log: RequestLog | None = None) -> Request:
    """最小 request stub：`request_log=None` 时覆盖标准库 logger 兜底路径。

    必须是 SimpleNamespace（无属性自动生成）：MagicMock 会把 `request_log` 兜成
    truthy mock，静默走错分支（见模块 docstring）。返回值 cast 成 `Request`
    只为过处理器签名（运行期只被读 `state.request_log` / 写 `exception_handled`）。
    """
    stub = SimpleNamespace(state=SimpleNamespace(request_log=request_log))
    return cast("Request", stub)


def _raw_bytes(resp: Response) -> bytes:
    """响应体字节：starlette 存根把 `body` 标为 `bytes | memoryview`，统一归一为 bytes。"""
    raw = resp.body
    return raw.tobytes() if isinstance(raw, memoryview) else bytes(raw)


def _json_body(resp: Response) -> dict[str, Any]:
    """处理器产出的 JSON 响应体（直 await 分支用，不经过 TestClient）。"""
    return json.loads(_raw_bytes(resp))


def _body_text(resp: Response) -> str:
    """响应体文本（哨兵断言「不泄漏」用）。"""
    return _raw_bytes(resp).decode()


def _remove_test_route(path: str) -> None:
    """移除测试临时注册的路由，不污染其他用例。"""
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != path]


def _records_at(records: list[Record], level: str) -> list[Record]:
    """按 loguru 级别名过滤捕获到的原始 record。"""
    return [r for r in records if r["level"].name == level]


# ---- _lookup：MRO 查表 ----


def test_lookup_hits_registered_type_exactly():
    assert _lookup(SessionNotFound(message="x")) == ("SESSION_NOT_FOUND", 404, "会话不存在")


def test_lookup_message_empty_carries_contract_code():
    """空消息是独立契约码（AC-001-4）：不能与 INVALID_ARGUMENT 混同。"""
    assert _lookup(MessageEmpty(message="x")) == ("MESSAGE_EMPTY", 422, "消息内容不能为空")


def test_lookup_falls_back_to_nearest_registered_parent():
    """现场定义的未注册领域异常：MRO 里没有自己，回退到 Exception 兜底配置。"""

    class UnregisteredError(BusinessException):
        """未在 ERROR_MAP 注册的子类。"""

    assert _lookup(UnregisteredError(message="x")) == ERROR_MAP[Exception]
    assert ERROR_MAP[Exception] == ("INTERNAL", 500, "服务器内部错误")


def test_lookup_defensive_fallback_when_exception_not_in_mro():
    """防御分支：MRO 里连 Exception 都没有的对象，也返回兜底配置（_lookup 最后一行）。"""
    assert _lookup(cast("BaseException", object())) == ("INTERNAL", 500, "服务器内部错误")


def test_exported_domain_exceptions_are_registered_in_error_map():
    """防漂移：exception 包导出的领域异常（除基类外）必须已在 ERROR_MAP 注册。"""
    for name in exception_pkg.__all__:
        exc_type = getattr(exception_pkg, name)
        # 基类本身不映射错误码，由子类各自注册
        if exc_type is BusinessException:
            continue
        assert exc_type in ERROR_MAP, f"{name} 未在 ERROR_MAP 注册"


# ---- exception_handler：直 await 各分支的响应（无 request_log → 标准库兜底出日志）----


async def test_handler_protocol_error_uses_friendly_message():
    resp = await exception_handler(_request_with_log(), HTTPException(status_code=404))
    assert resp.status_code == 404
    assert _json_body(resp) == {"code": "404", "message": "路径不存在"}


async def test_handler_protocol_error_falls_back_to_detail():
    resp = await exception_handler(
        _request_with_log(), HTTPException(status_code=418, detail="teapot")
    )
    assert resp.status_code == 418
    assert _json_body(resp) == {"code": "418", "message": "teapot"}


async def test_handler_protocol_error_falls_back_to_starlette_detail():
    """协议表未收录的状态码回退 detail：starlette 会把缺省 detail 归一为 HTTP 短语并原样透传。"""
    exc = HTTPException(status_code=400)
    resp = await exception_handler(_request_with_log(), exc)
    assert resp.status_code == 400
    body = _json_body(resp)
    assert body["code"] == "400"
    assert body["message"] == exc.detail


async def test_handler_protocol_error_with_unusable_detail_yields_empty_message():
    """防御分支：detail 为 None / 非字符串（下游改写或自定义子类）时归一为空串。"""
    for bad_detail in (None, {"nested": "object"}):
        exc = HTTPException(status_code=400)
        exc.detail = bad_detail  # type: ignore[assignment]
        resp = await exception_handler(_request_with_log(), exc)
        assert _json_body(resp) == {"code": "400", "message": ""}


async def test_handler_business_exception_uses_message_and_contract_code():
    exc = SessionNotFound(message="会话不存在")
    resp = await exception_handler(_request_with_log(), exc)
    assert resp.status_code == 404
    assert _json_body(resp) == {"code": "SESSION_NOT_FOUND", "message": "会话不存在"}


async def test_handler_validation_error_formats_locations():
    error = {
        "loc": ("query", "n"),
        "msg": "Input should be a valid integer",
        "type": "int_parsing",
    }
    resp = await exception_handler(_request_with_log(), RequestValidationError([error]))
    assert resp.status_code == 422
    body = _json_body(resp)
    assert body["code"] == "INVALID_ARGUMENT"
    assert body["message"] == "query.n: Input should be a valid integer"


async def test_handler_unknown_exception_does_not_leak_internals(caplog):
    """哨兵断言：未知异常走 ERROR 且带栈（进日志）、内部信息绝不进响应体。"""
    with caplog.at_level(logging.ERROR, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), Exception("secret internal detail"))
    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].exc_info is not None  # 未知异常必须带栈，供根因排查
    assert resp.status_code == 500
    body = _json_body(resp)
    assert body == {"code": "INTERNAL", "message": "服务器内部错误"}
    assert "secret" not in _body_text(resp)
    assert "secret internal detail" in caplog.text


# ---- 日志出口之一：有 request_log → loguru 单管线（补状态码 + 按级落）----


async def test_handler_protocol_error_logs_warning_via_request_log(log_records):
    log = RequestLog(message="请求完成", method="GET", path="/nope")
    resp = await exception_handler(_request_with_log(log), HTTPException(status_code=404))

    assert resp.status_code == 404
    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "请求异常: 路径不存在"
    assert record["extra"]["status_code"] == 404  # 处理器补上真实状态码
    assert record["extra"]["path"] == "/nope"  # 请求上下文（request_id/method/path）随记录带出
    assert record["exception"] is None  # 协议错误是预期控制流，不带栈


async def test_handler_validation_error_logs_warning_via_request_log(log_records):
    """校验失败（INVALID_ARGUMENT/422）也经 request_log 落一条 warning（此前不进任何日志出口）。"""
    error = {
        "loc": ("query", "n"),
        "msg": "Input should be a valid integer",
        "type": "int_parsing",
    }
    log = RequestLog(message="请求完成")
    resp = await exception_handler(_request_with_log(log), RequestValidationError([error]))

    assert resp.status_code == 422
    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "请求异常: query.n: Input should be a valid integer"
    assert record["extra"]["status_code"] == 422
    assert record["exception"] is None


async def test_handler_business_exception_logs_detail_via_request_log(log_records):
    """detail 只进日志：同一哨兵值在日志里找得到、响应体里找不到；预期控制流走 WARNING 且不带栈。"""
    exc = SessionNotFound(
        message="会话不存在", detail="select * from sessions where id = 'secret-id'"
    )
    resp = await exception_handler(_request_with_log(RequestLog(message="请求完成")), exc)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert (
        record["message"]
        == "业务异常: 会话不存在 | detail=select * from sessions where id = 'secret-id'"
    )
    assert record["exception"] is None  # 预期控制流不带栈，避免应用日志层再造噪音
    assert _json_body(resp) == {"code": "SESSION_NOT_FOUND", "message": "会话不存在"}
    assert "secret-id" not in _body_text(resp)


async def test_handler_business_exception_with_original_exception_logs_stack(log_records):
    """包装了基础设施故障（original_exception）时，WARNING 日志才附原始异常栈。"""
    exc = SessionNotFound(
        message="数据保存失败",
        detail="unique_violation",
        original_exception=ValueError("db boom"),
    )
    await exception_handler(_request_with_log(RequestLog(message="请求完成")), exc)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "业务异常: 数据保存失败 | detail=unique_violation"
    assert record["exception"] is not None  # 根因（原始异常）带栈
    assert isinstance(record["exception"].value, ValueError)


async def test_handler_unknown_exception_logs_error_with_stack(log_records):
    """未知异常走 ERROR 且带栈（进日志），内部信息绝不进响应体。"""
    resp = await exception_handler(
        _request_with_log(RequestLog(message="请求完成")), Exception("secret internal detail")
    )

    record = log_records[-1]
    assert record["level"].name == "ERROR"
    assert record["message"] == "未处理异常: secret internal detail"
    assert record["exception"] is not None  # 未知异常必须带栈，供根因排查
    assert record["extra"]["status_code"] == 500
    assert "secret" not in _body_text(resp)


# ---- 日志出口之二：request_log 缺失 → 标准库 logger 兜底（同口径）----


async def test_handler_business_exception_detail_is_logged_not_leaked(caplog):
    """detail 只进日志：同一哨兵值在日志里找得到、响应体里找不到；预期控制流走 WARNING 且不带栈。"""
    exc = SessionNotFound(
        message="会话不存在", detail="select * from sessions where id = 'secret-id'"
    )
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), exc)
    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None  # 预期控制流不带栈，避免应用日志层再造噪音
    assert "secret-id" in caplog.text
    assert _json_body(resp) == {"code": "SESSION_NOT_FOUND", "message": "会话不存在"}
    assert "secret-id" not in _body_text(resp)


async def test_handler_business_exception_with_original_exception_logs_traceback(caplog):
    """包装了基础设施故障（original_exception）时，WARNING 日志才附原始异常栈。"""
    exc = SessionNotFound(
        message="数据保存失败",
        detail="unique_violation",
        original_exception=ValueError("db boom"),
    )
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        await exception_handler(_request_with_log(), exc)
    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is not None  # 根因（原始异常）带栈


async def test_handler_stdlib_fallback_warns_for_protocol_error(caplog):
    """不在中间件链内直调处理器（无 request_log）时回退标准库 logger，severity 口径一致。"""
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), HTTPException(status_code=404))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None
    assert "请求异常: 路径不存在" in caplog.text
    assert _json_body(resp) == {"code": "404", "message": "路径不存在"}


async def test_handler_stdlib_fallback_omits_empty_detail_suffix(caplog):
    """兜底路径的消息口径与 loguru 分支一致：detail 为空时不追加 " | detail=" 尾巴。"""
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        await exception_handler(_request_with_log(), SessionNotFound(message="会话不存在"))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert "业务异常: 会话不存在" in caplog.text
    assert "| detail=" not in caplog.text  # 条件拼接：detail 为空就没有尾巴


async def test_handler_stdlib_fallback_errors_with_stack_for_unknown(caplog):
    with caplog.at_level(logging.ERROR, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), Exception("boom"))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].exc_info is not None
    assert resp.status_code == 500


# ---- TestClient：走 main.py 的真实注册链路（响应 + 日志两条断言线）----


def test_nonexistent_path_returns_unified_error_body():
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/nonexistent")
    assert resp.status_code == 404
    assert resp.json() == {"code": "404", "message": "路径不存在"}


def test_client_404_logs_exactly_one_warning(log_records):
    """404：恰一条 warning（经 request_log），且没有 SUCCESS（异常侧不重复记请求完成）。"""
    resp = TestClient(app, raise_server_exceptions=False).get("/nonexistent")
    assert resp.status_code == 404

    warnings = _records_at(log_records, "WARNING")
    assert len(warnings) == 1
    assert warnings[0]["message"] == "请求异常: 路径不存在"
    assert warnings[0]["extra"]["status_code"] == 404
    assert _records_at(log_records, "SUCCESS") == []


def test_invalid_query_returns_validation_error_code():
    @app.get("/_test_validate")
    async def _validate(n: int):
        return {"n": n}

    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.get("/_test_validate", params={"n": "not-an-int"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "INVALID_ARGUMENT"
        assert "query.n" in body["message"]
        assert client.get("/_test_validate", params={"n": 3}).json() == {"n": 3}
    finally:
        _remove_test_route("/_test_validate")


def test_client_422_logs_exactly_one_warning(log_records):
    @app.get("/_test_validate_log")
    async def _validate_log(n: int):
        return {"n": n}

    try:
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/_test_validate_log", params={"n": "not-an-int"}
        )
        assert resp.status_code == 422

        warnings = _records_at(log_records, "WARNING")
        assert len(warnings) == 1
        assert warnings[0]["message"].startswith("请求异常: query.n")
        assert warnings[0]["extra"]["status_code"] == 422
        assert _records_at(log_records, "SUCCESS") == []
    finally:
        _remove_test_route("/_test_validate_log")


def test_business_exception_route_works_with_default_test_client():
    """BusinessException 已显式注册：默认 TestClient（raise_server_exceptions=True）
    不抛异常，直接拿到统一 JSON（回归守卫：漏注册会冒泡到 ServerErrorMiddleware 并 re-raise）。"""

    @app.get("/_test_not_found")
    async def _not_found():
        raise SessionNotFound(message="会话不存在")

    client = TestClient(app)  # 故意保留默认 raise_server_exceptions=True
    try:
        resp = client.get("/_test_not_found")
        assert resp.status_code == 404
        assert resp.json() == {"code": "SESSION_NOT_FOUND", "message": "会话不存在"}
    finally:
        _remove_test_route("/_test_not_found")


def test_uncaught_exception_returns_internal():
    @app.get("/_test_boom")
    async def _boom():
        raise Exception("secret internal detail")  # 故意触发兜底处理器

    # 未知异常走 ServerErrorMiddleware（构造响应后仍会 re-raise），必须关掉 raise_server_exceptions
    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.get("/_test_boom")
        assert resp.status_code == 500
        assert resp.json() == {"code": "INTERNAL", "message": "服务器内部错误"}
        assert "secret" not in resp.text  # TestClient 响应（httpx2）自带 .text
    finally:
        _remove_test_route("/_test_boom")


def test_client_500_logs_exactly_one_error_with_stack(log_records):
    @app.get("/_test_boom_log")
    async def _boom_log():
        raise Exception("secret internal detail")

    try:
        resp = TestClient(app, raise_server_exceptions=False).get("/_test_boom_log")
        assert resp.status_code == 500

        errors = _records_at(log_records, "ERROR")
        assert len(errors) == 1
        assert errors[0]["message"] == "未处理异常: secret internal detail"
        assert errors[0]["exception"] is not None  # 带栈
        assert errors[0]["extra"]["status_code"] == 500
        assert _records_at(log_records, "SUCCESS") == []
        assert "secret" not in resp.text
    finally:
        _remove_test_route("/_test_boom_log")


def test_client_business_exception_logs_exactly_one_warning(log_records):
    """业务异常（TestClient 真实链路）：恰一条 warning（经 request_log、无栈），
    没有 SUCCESS——异常侧由处理器落请求日志，中间件不重复记（与 404/422/500 用例同型）。"""

    @app.get("/_test_not_found_log")
    async def _not_found_log():
        raise SessionNotFound(message="会话不存在")

    try:
        resp = TestClient(app).get("/_test_not_found_log")
        assert resp.status_code == 404
        assert resp.json() == {"code": "SESSION_NOT_FOUND", "message": "会话不存在"}

        warnings = _records_at(log_records, "WARNING")
        assert len(warnings) == 1
        assert "业务异常" in warnings[0]["message"]
        assert warnings[0]["message"].endswith("会话不存在")
        assert warnings[0]["extra"]["status_code"] == 404
        assert warnings[0]["exception"] is None  # 预期控制流不带栈
        assert _records_at(log_records, "SUCCESS") == []
    finally:
        _remove_test_route("/_test_not_found_log")


# ---- 错误码文档 ----


def test_generate_error_docs_contains_both_tables():
    docs = generate_error_docs()
    assert "## 错误码说明" in docs
    # 业务错误表 + 协议错误表各一张
    assert docs.count("| 业务错误码 | HTTP状态码 | 消息 |") == 2
    assert "| SESSION_NOT_FOUND | 404 | 会话不存在 |" in docs
    assert "| 404 | 404 | 路径不存在 |" in docs


def test_generate_error_docs_dedupes_shared_code_and_hides_fallback():
    docs = generate_error_docs()
    # RequestValidationError 与 InvalidArgument 同码（INVALID_ARGUMENT），只列一次
    assert docs.count("INVALID_ARGUMENT") == 1
    assert "INTERNAL" not in docs  # Exception 兜底行不进文档


def test_app_description_includes_error_docs():
    assert "## 错误码说明" in app.description
