"""unit 层：/health/ready 就绪探针（不真的连库，两个分支都用假引擎覆盖）。

- 数据库可达 → 200 + {"status": "ok", "database": "connected"}；
- 数据库不可达（连接抛错）→ 200 + {"status": "not ready", "database": "disconnected"}，
  不抛异常、不返回 5xx（探针语义是「答未就绪」，不是「接口出错」）。

假引擎 patch 点是 `app.main.get_engine`（main.py 从 app.core.db 引入该符号）。
"""

from fastapi.testclient import TestClient

from app.main import app


class _FakeConnection:
    """最小异步连接：记录执行的语句并支持 async with。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, statement: object, *args: object) -> None:
        # TextClause 记录其原始 SQL 文本（str() 需要方言，这里取 .text 属性）
        self.executed.append(str(getattr(statement, "text", statement)))

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _ConnectedEngine:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    def connect(self) -> _FakeConnection:
        return self.connection


class _BrokenEngine:
    """模拟无库/连接失败：connect() 即抛错。"""

    def connect(self) -> _FakeConnection:
        raise RuntimeError("connection refused")


def test_health_ready_reports_connected(monkeypatch):
    engine = _ConnectedEngine()
    monkeypatch.setattr("app.main.get_engine", lambda: engine)

    resp = TestClient(app).get("/health/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "connected"}
    assert engine.connection.executed == ["SELECT 1"]  # 真的发了探活 SQL


def test_health_ready_reports_not_ready_when_db_unreachable(monkeypatch):
    monkeypatch.setattr("app.main.get_engine", lambda: _BrokenEngine())

    resp = TestClient(app).get("/health/ready")

    assert resp.status_code == 200  # 不用 5xx：避免编排/监控把「未就绪」误判为接口故障
    assert resp.json() == {"status": "not ready", "database": "disconnected"}


def test_health_ready_is_outside_api_prefix_so_not_wrapped(monkeypatch):
    monkeypatch.setattr("app.main.get_engine", lambda: _ConnectedEngine())

    body = TestClient(app).get("/health/ready").json()

    assert "code" not in body  # 就绪探针不套 /api/ 成功信封
