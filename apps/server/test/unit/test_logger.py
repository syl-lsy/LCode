"""unit 层：loguru 日志体系（app/core/logger/，基座铁律 11 / NFR-007）。

覆盖点：
- `setup_logging` 双环境分支：production 只挂 stdout 单行 JSON（serialize=True，级别走
  LOG_LEVEL）；development 挂彩色控制台 + ERROR 文件 sink（LOG_FILE_DIR/errors.log，
  10 MB 轮转、保留 7 天、格式串带 {exception}）；
- `LogRecord._emit`：extra 过滤假值（未取到值的字段不落盘）、duration_ms 兜底
  （构造到落日志的实际耗时）、request_id 取 contextvar（缺省 "-"）；
- `warning(exc=...)` 带栈——「业务异常仅 original_exception 存在时才带栈」的机制基础；
- 七级方法各自转发到对应级别；
- `service_logger` 分支：成功（含 id_extractor 提取 entity_id）/ BusinessException
  →warning 无栈 / 其他异常→error 带栈且 re-raise / id_extractor 抛错静默忽略；
  失败路径也尝试 id_extractor(args, kwargs, None)；
- patcher 兜底：setup_logging 后直接 logger.info() 不因格式串取键失败丢记录，
  且不覆盖显式 bind 的值；
- uvicorn access/error 标准库日志关闭（模块级 + lifespan 启动钩子再兜一次）；
- 请求日志「每请求恰一条」中间件级集成：/health 成功恰一条 SUCCESS（带 request_id
  与状态码）、/nonexistent 404 恰一条 warning（handler 路径，不重复记 SUCCESS）。

loguru 捕获 fixture（log_records）统一定义在 test/unit/conftest.py，全 unit 层共用。
"""

import logging
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.core.config import common_settings, log_settings
from app.core.logger import (
    DBLog,
    LogRecord,
    RequestLog,
    request_id_var,
    service_logger,
    setup_logging,
)
from app.core.logger.log_record import LogLevel
from app.exception import SessionNotFound
from app.main import app

if TYPE_CHECKING:
    # loguru 的 Record 是 TypedDict，只在类型存根里（运行时不可 import）
    from loguru import Record

# ---- setup_logging：dev / production 双分支 ----


def test_setup_logging_production_uses_single_json_sink(monkeypatch):
    """production：清默认 sink → 只挂 stdout、serialize=True（单行 JSON）、级别走 LOG_LEVEL。"""
    mock_logger = MagicMock()
    monkeypatch.setattr("app.core.logger.log_record.logger", mock_logger)
    monkeypatch.setattr(common_settings, "environment", "production")
    monkeypatch.setattr(log_settings, "level", "WARNING")

    setup_logging()

    mock_logger.remove.assert_called_once_with()  # 清掉 loguru 默认 stderr sink
    mock_logger.configure.assert_called_once()  # patcher（_ensure_extras）已装
    assert mock_logger.add.call_count == 1
    args, kwargs = mock_logger.add.call_args
    assert args[0] is sys.stdout
    assert kwargs["serialize"] is True
    # ADR-006 安全承诺：diagnose=False 防出错行局部变量值随生产 JSON 外发（NFR-004），
    # 显式守卫防将来重构丢失（loguru 默认 diagnose/backtrace 全开）
    assert kwargs["diagnose"] is False
    assert kwargs["level"] == "WARNING"


def test_setup_logging_development_adds_console_and_error_file_sink(monkeypatch, tmp_path):
    """development：彩色控制台 + ERROR 文件 sink（目录自动创建、轮转与保留策略齐备）。"""
    mock_logger = MagicMock()
    monkeypatch.setattr("app.core.logger.log_record.logger", mock_logger)
    monkeypatch.setattr(common_settings, "environment", "development")
    monkeypatch.setattr(log_settings, "file_dir", str(tmp_path / "logs"))

    setup_logging()

    assert mock_logger.add.call_count == 2
    console_args, console_kwargs = mock_logger.add.call_args_list[0]
    assert console_args[0] is sys.stdout
    assert console_kwargs["colorize"] is True
    assert "{extra[request_id]}" in console_kwargs["format"]
    assert "{extra[duration_ms]}" in console_kwargs["format"]

    file_args, file_kwargs = mock_logger.add.call_args_list[1]
    assert file_args[0].endswith("logs/errors.log")  # LOG_FILE_DIR 拼出的错误日志路径
    assert Path(file_args[0]).parent.is_dir()  # 目录已自动创建
    assert file_kwargs["level"] == "ERROR"
    assert file_kwargs["rotation"] == "10 MB"
    assert file_kwargs["retention"] == "7 days"
    assert "{exception}" in file_kwargs["format"]  # 错误栈进文件


# ---- LogRecord._emit：extra 过滤 / duration_ms 兜底 / request_id ----


def test_emit_filters_falsy_fields_and_fills_request_id(log_records):
    """extra 过滤假值：未取到值的字段不落盘；request_id 取当前请求的 contextvar。"""
    token = request_id_var.set("req-1")
    try:
        RequestLog(message="请求完成", method="GET", path="/health").success()
    finally:
        request_id_var.reset(token)

    record = log_records[-1]
    assert record["level"].name == "SUCCESS"
    assert record["message"] == "请求完成"
    assert record["extra"]["request_id"] == "req-1"
    assert record["extra"]["duration_ms"] >= 0  # 构造到落日志的耗时兜底（保留 2 位）
    assert record["extra"]["method"] == "GET"
    assert record["extra"]["path"] == "/health"
    assert "status_code" not in record["extra"]  # 0 是假值：没拿到状态码就不落盘
    assert "client_ip" not in record["extra"]


def test_emit_falls_back_to_dash_without_request_context(log_records):
    """无请求上下文（如 agent 运行时后台任务）时 request_id 缺省为 "-"，不因取不到而报错。"""
    token = request_id_var.set("")  # token 模式：用例结束后恢复，不泄漏给后续用例
    try:
        LogRecord(message="无请求上下文").info()
    finally:
        request_id_var.reset(token)

    assert log_records[-1]["extra"]["request_id"] == "-"


def test_emit_keeps_explicit_duration_ms(log_records):
    """显式给了 duration_ms 就用它（如慢查询的真实 SQL 耗时），不覆盖为兜底耗时。"""
    DBLog(message="慢查询", sql="SELECT 1", params="()", duration_ms=321.5).warning()

    record = log_records[-1]
    assert record["extra"]["duration_ms"] == 321.5
    assert record["extra"]["sql"] == "SELECT 1"
    assert record["extra"]["params"] == "()"


def test_warning_with_exception_carries_stack(log_records):
    try:
        raise ValueError("db boom")
    except ValueError as exc:
        LogRecord(message="业务异常: 会话保存失败").warning(exc=exc)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["exception"] is not None
    assert isinstance(record["exception"].value, ValueError)


def test_warning_without_exception_has_no_stack(log_records):
    """预期控制流（业务异常）不带栈：record["exception"] 为空，不制造 traceback 噪音。"""
    LogRecord(message="业务异常: 会话不存在").warning()

    assert log_records[-1]["exception"] is None


def test_all_seven_levels_are_forwarded(log_records):
    record = LogRecord(message="x")
    for level in LogLevel:
        getattr(record, level.lower())()

    assert [r["level"].name for r in log_records] == [level.value for level in LogLevel]


# ---- service_logger：业务操作日志装饰器 ----


async def test_service_logger_success_extracts_entity_id(log_records):
    @service_logger(
        "创建会话", entity="session", id_extractor=lambda args, kwargs, result: result["id"]
    )
    async def _create() -> dict[str, int]:
        return {"id": 42}

    assert await _create() == {"id": 42}

    record = log_records[-1]
    assert record["level"].name == "SUCCESS"
    assert record["message"] == "创建会话成功"
    assert record["extra"]["action"] == "创建会话"
    assert record["extra"]["entity"] == "session"
    assert record["extra"]["entity_id"] == 42


async def test_service_logger_business_exception_warns_without_stack(log_records):
    @service_logger("删除会话")
    async def _delete() -> None:
        raise SessionNotFound(message="会话不存在")

    with pytest.raises(SessionNotFound):
        await _delete()

    record = log_records[-1]
    assert record["level"].name == "WARNING"  # 业务异常=预期控制流
    assert record["message"] == "删除会话失败"
    assert record["exception"] is None  # 不带栈


async def test_service_logger_unexpected_exception_errors_with_stack(log_records):
    @service_logger("发起运行")
    async def _run() -> None:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await _run()

    record = log_records[-1]
    assert record["level"].name == "ERROR"
    assert record["message"] == "发起运行失败"
    assert record["exception"] is not None
    assert record["exception"].value.args == ("db down",)


async def test_service_logger_swallows_id_extractor_errors(log_records):
    """id_extractor 抛错静默忽略：日志绝不因提取 id 失败而影响业务结果。"""

    def _boom(args: tuple, kwargs: dict, result: object) -> str:
        raise RuntimeError("no id here")

    @service_logger("创建会话", id_extractor=_boom)
    async def _create() -> str:
        return "ok"

    assert await _create() == "ok"
    record = log_records[-1]
    assert record["level"].name == "SUCCESS"
    assert "entity_id" not in record["extra"]


async def test_service_logger_failure_path_also_attempts_id_extractor(log_records):
    """失败路径也尝试 id_extractor(args, kwargs, None)——能从入参取到 id 就带上。"""
    seen_results: list[object] = []

    def _extract(args: tuple, kwargs: dict, result: object) -> str:
        seen_results.append(result)
        return "run-9527" if result is None else "wrong"

    @service_logger("发起运行", id_extractor=_extract)
    async def _run() -> None:
        raise SessionNotFound(message="会话不存在")

    with pytest.raises(SessionNotFound):
        await _run()

    assert seen_results == [None]  # 失败时 result 传 None
    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["extra"]["entity_id"] == "run-9527"


async def test_service_logger_failure_path_swallows_id_extractor_errors(log_records):
    """失败路径再抛错也不许冒泡：异常日志照常落（提取失败静默忽略）。"""

    def _boom(args: tuple, kwargs: dict, result: object) -> str:
        raise RuntimeError("no id here")

    @service_logger("删除会话", id_extractor=_boom)
    async def _delete() -> None:
        raise SessionNotFound(message="会话不存在")

    with pytest.raises(SessionNotFound):
        await _delete()

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "删除会话失败"
    assert "entity_id" not in record["extra"]


# ---- uvicorn 标准库日志关闭（日志单管线）----


def test_lifespan_re_silences_uvicorn_loggers():
    """启动钩子再关一次 uvicorn logger：uvicorn 每次启动都会用 dictConfig 重置 disabled。

    回归守卫：仅在模块级关一次是不够的（`fastapi dev` 先导入应用再配置 uvicorn 日志），
    进入 lifespan 后访问日志/错误日志必须保持关闭，请求日志只由 logging 中间件出。
    """
    logging.getLogger("uvicorn.error").disabled = False  # 模拟被 uvicorn 配置重置
    logging.getLogger("uvicorn.access").disabled = False

    with TestClient(app) as client:  # 上下文管理器形式才会触发 lifespan 启动钩子
        assert client.get("/health").status_code == 200
        assert logging.getLogger("uvicorn.error").disabled is True
        assert logging.getLogger("uvicorn.access").disabled is True


# ---- patcher 兜底：直接 logger.info() 不丢记录 ----


def test_patcher_keeps_direct_logger_calls_alive(monkeypatch, tmp_path):
    """setup_logging 装 patcher 后直接 logger.info()：extra 默认键齐备，记录不丢。

    setup_logging 会 logger.remove() 清掉全部 sink（含 fixture 的捕获 sink），
    因此本用例在 setup_logging 之后自建 sink；它在真实 loguru 全局上装了输出 sink，
    收尾再 logger.remove() 清掉——monkeypatch 管不到 loguru 的全局状态，不清会残留到后续用例。
    """
    monkeypatch.setattr(log_settings, "file_dir", str(tmp_path / "logs"))
    setup_logging()

    records: list[Record] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    assert sink_id is not None  # 仅确认注册成功；真实断言在下方 records
    try:
        logger.info("直接调用")  # 不经过 LogRecord：格式串引用的 extra 键靠 patcher 兜底
        logger.bind(request_id="rid-1", duration_ms=12.5).info("显式绑定")
    finally:
        logger.remove()  # 清掉 setup_logging 装的 stdout/文件 sink 与本用例的捕获 sink

    assert len(records) == 2
    assert records[0]["extra"]["request_id"] == "-"
    assert records[0]["extra"]["duration_ms"] == "-"
    assert records[1]["extra"]["request_id"] == "rid-1"  # setdefault 不覆盖显式值
    assert records[1]["extra"]["duration_ms"] == 12.5


# ---- 请求日志「每请求恰一条」：中间件级集成（成功走中间件 / 异常走处理器）----


def _records_at(records: list[Record], level: str) -> list[Record]:
    """按 loguru 级别名过滤捕获到的原始 record。"""
    return [r for r in records if r["level"].name == level]


def test_health_success_emits_exactly_one_success_log(log_records):
    """成功路径恰一条 SUCCESS：带 uuid4 request_id、补齐的真实状态码与 method/path。"""
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200

    success = _records_at(log_records, "SUCCESS")
    assert len(success) == 1
    record = success[0]
    assert record["message"] == "请求完成"
    assert uuid.UUID(record["extra"]["request_id"]).version == 4  # request_id 是 uuid4
    assert record["extra"]["status_code"] == 200  # 中间件在 call_next 返回后补齐
    assert record["extra"]["method"] == "GET"
    assert record["extra"]["path"] == "/health"


def test_nonexistent_404_logs_exactly_one_warning(log_records):
    """/nonexistent 404 恰一条 warning（异常处理器经 request_log 落），无 SUCCESS 重复记。"""
    resp = TestClient(app, raise_server_exceptions=False).get("/nonexistent")
    assert resp.status_code == 404

    warnings = _records_at(log_records, "WARNING")
    assert len(warnings) == 1
    assert warnings[0]["message"] == "请求异常: 路径不存在"
    assert warnings[0]["extra"]["status_code"] == 404
    assert _records_at(log_records, "SUCCESS") == []  # 异常侧不重复记请求完成
