"""unit 层：请求级会话依赖 `dependencies.get_db`（基座铁律 10 的落点）。

事务边界只有两种结局，两个分支都要断言：正常 → commit；异常 → rollback 且异常上抛
（让异常处理器出统一错误体）。fake 掉会话工厂，不连库、零网络。
"""

from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import dependencies as deps_module
from app.dependencies import get_db


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.entered = 0

    def __call__(self) -> _FakeSessionCtx:
        return _FakeSessionCtx(self)


class _FakeSessionCtx:
    def __init__(self, factory: _FakeFactory) -> None:
        self.factory = factory

    async def __aenter__(self) -> _FakeSession:
        self.factory.entered += 1
        return self.factory.session

    async def __aexit__(self, *exc: object) -> bool:
        await self.factory.session.close()
        return False


def _as_generator(
    dependency: AsyncIterator[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """按异步生成器看待依赖（`athrow` 只在生成器协议上存在，`get_db` 就是生成器）。"""
    return cast("AsyncGenerator[AsyncSession]", dependency)


async def test_get_db_commits_on_success(monkeypatch):
    session = _FakeSession()
    factory = _FakeFactory(session)
    monkeypatch.setattr(deps_module, "get_session_factory", lambda: factory)

    dependency = _as_generator(get_db())
    await dependency.__anext__()
    with pytest.raises(StopAsyncIteration):
        await dependency.__anext__()  # 推进到 yield 之后：等价于一个请求正常走完

    assert session.events == ["commit", "close"]  # 请求成功：提交后关闭
    assert factory.entered == 1


async def test_get_db_rolls_back_and_propagates_on_error(monkeypatch):
    session = _FakeSession()
    factory = _FakeFactory(session)
    monkeypatch.setattr(deps_module, "get_session_factory", lambda: factory)

    dependency = _as_generator(get_db())
    await dependency.__anext__()
    with pytest.raises(RuntimeError, match="boom"):
        await dependency.athrow(RuntimeError("boom"))

    assert session.events == ["rollback", "close"]  # 异常：回滚后关闭
