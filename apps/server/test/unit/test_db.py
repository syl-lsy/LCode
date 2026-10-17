"""unit 层：`app/core/db.py`（ADR-004 偏差 2 的落点，替代基座的 test_engine.py）。

只构造引擎/工厂对象，**不发起真实连接**（零网络）；目的之一是保证 `--cov=app`
下 core/db.py 无遗漏分支（懒加载单例、URL 构造、dispose 归零、SQL 慢查询事件钩子）。
"""

import time

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.pool import QueuePool

from app.core import db as db_module
from app.core.config import db_settings, log_settings
from app.core.db import (
    _after_cursor_execute,
    _before_cursor_execute,
    _query_start_times,
    build_database_url,
    dispose_engine,
    get_engine,
    get_session_factory,
    session_scope,
)


async def _cleanup() -> None:
    """每个用例收尾释放引擎（测试间不共享连接池状态）。"""
    await dispose_engine()


def _fill_db_settings(monkeypatch) -> None:
    """补齐连接参数，避免空端口导致 URL 解析失败（create_async_engine 不建立连接）。"""
    monkeypatch.setattr(db_settings, "host", "localhost")
    monkeypatch.setattr(db_settings, "port", 5432)
    monkeypatch.setattr(db_settings, "user", "user")
    monkeypatch.setattr(db_settings, "password", "password")
    monkeypatch.setattr(db_settings, "name", "test_db")


async def test_get_engine_returns_singleton(monkeypatch):
    _fill_db_settings(monkeypatch)

    engine = get_engine()
    assert engine is get_engine()  # 懒加载单例：重复调用返回同一对象
    assert engine.url.database == "test_db"
    # 慢查询钩子已真挂在 sync_engine 上（事件名拼错/挂错对象会静默失效，
    # 按「事件名 + 钩子函数」显式守卫；异步引擎统一经 sync_engine 触发事件）
    assert event.contains(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    assert event.contains(engine.sync_engine, "after_cursor_execute", _after_cursor_execute)

    await _cleanup()


async def test_get_engine_uses_pool_settings_from_config(monkeypatch):
    """连接池参数集中在 DB_ 配置组（基座铁律 2），引擎按配置构造。"""
    _fill_db_settings(monkeypatch)
    monkeypatch.setattr(db_settings, "pool_size", 3)
    monkeypatch.setattr(db_settings, "max_overflow", 4)

    engine = get_engine()

    # 断言具体池型（QueuePool）后取 size：基类 Pool 没有 size()，类型收窄也顺带验证池配置
    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == 3
    await _cleanup()


def test_build_database_url_escapes_special_characters(monkeypatch):
    """URL.create 口径：密码里的 @ / : / % 等字符必须转义（手工 f-string 会拼出错误 host）。"""
    monkeypatch.setattr(db_settings, "user", "us er")
    monkeypatch.setattr(db_settings, "password", "p@ss:w/rd")
    monkeypatch.setattr(db_settings, "host", "db.internal")
    monkeypatch.setattr(db_settings, "port", 6543)
    monkeypatch.setattr(db_settings, "name", "lcode")

    url = build_database_url()

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "db.internal"
    assert url.port == 6543
    assert url.username == "us er"
    assert url.password == "p@ss:w/rd"
    assert "p%40ss" in url.render_as_string(hide_password=False)  # 渲染时被 percent-encode


async def test_get_session_factory_is_singleton_bound_to_engine(monkeypatch):
    _fill_db_settings(monkeypatch)

    factory = get_session_factory()
    assert factory is get_session_factory()  # 工厂单例（agent 运行时与请求级共用）
    assert factory.kw["bind"] is get_engine()  # 绑定到引擎单例
    assert factory.kw["expire_on_commit"] is False  # commit 后对象仍可读

    await _cleanup()


async def test_session_scope_commits_and_closes(monkeypatch):
    """`session_scope`（agent 运行时短事务）：正常退出 commit，退出即关闭。"""
    events: list[str] = []

    class _FakeSession:
        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    class _FakeFactory:
        def __call__(self) -> _FakeSessionCtx:
            return _FakeSessionCtx(_FakeSession())

    class _FakeSessionCtx:
        def __init__(self, session: _FakeSession) -> None:
            self._session = session

        async def __aenter__(self) -> _FakeSession:
            events.append("enter")
            return self._session

        async def __aexit__(self, *exc: object) -> bool:
            await self._session.close()
            return False

    monkeypatch.setattr(db_module, "get_session_factory", lambda: _FakeFactory())

    async with session_scope() as session:
        assert isinstance(session, _FakeSession)

    assert events == ["enter", "commit", "close"]


async def test_session_scope_rolls_back_on_error(monkeypatch):
    """事务语义：body 抛错时 rollback（不留下半成品事务），异常继续上抛由调用方处理。"""
    events: list[str] = []

    class _FakeSession:
        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    class _FakeSessionCtx:
        async def __aenter__(self) -> _FakeSession:
            return _FakeSession()

        async def __aexit__(self, *exc: object) -> bool:
            events.append("close")
            return False

    monkeypatch.setattr(db_module, "get_session_factory", lambda: _FakeSessionCtx)

    try:
        async with session_scope():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert events == ["rollback", "close"]


async def test_dispose_engine_resets_singletons(monkeypatch):
    """shutdown 路径：dispose 后单例归零，下次 get_engine 重建（不泄漏连接池）。"""
    _fill_db_settings(monkeypatch)
    first = get_engine()
    assert get_session_factory() is not None

    await dispose_engine()

    assert db_module._engine is None
    second = get_engine()
    assert second is not first
    await _cleanup()


async def test_dispose_engine_is_noop_without_engine():
    """未构造过引擎时 dispose 是空操作（应用启动失败/短命进程的清理路径）。"""
    await dispose_engine()
    await dispose_engine()  # 第二次仍不报错
    assert db_module._engine is None


# ---- SQL 事件钩子：慢查询阈值分档（直调模块级函数，不连库）----


def test_db_hooks_log_debug_for_normal_query(log_records):
    """未达阈值（默认 LOG_SLOW_QUERY_THRESHOLD=200ms）的 SQL 走 DEBUG（INFO 级别下不落盘）。"""
    context = object()  # 执行上下文：钩子按 id(context) 配对起点
    _before_cursor_execute(None, None, "SELECT 1", (), context, False)
    _after_cursor_execute(None, None, "SELECT 1", (), context, False)

    record = log_records[-1]
    assert record["level"].name == "DEBUG"
    assert record["message"] == "SQL执行"
    assert record["extra"]["sql"] == "SELECT 1"
    assert record["extra"]["duration_ms"] >= 0
    assert id(context) not in _query_start_times  # 起点已配对删除，映射表不泄漏


def test_db_hooks_log_warning_for_slow_query(log_records):
    """达阈值走 WARNING（慢查询）：DBLog 字段含 sql/params/duration_ms（基座铁律 11）。"""
    context = object()
    _query_start_times[id(context)] = time.perf_counter() - 1  # 起点改早 1 秒：必超 200ms

    _after_cursor_execute(None, None, "SELECT pg_sleep(1)", "()", context, False)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "慢查询"
    assert record["extra"]["sql"] == "SELECT pg_sleep(1)"
    assert record["extra"]["params"] == "()"
    assert record["extra"]["duration_ms"] >= log_settings.slow_query_threshold
    assert id(context) not in _query_start_times


def test_db_hooks_ignore_unpaired_execution(log_records):
    """没有配对的执行前事件（如外部直接触发）：静默跳过，不误报耗时。"""
    _after_cursor_execute(None, None, "SELECT 1", (), object(), False)

    assert log_records == []
