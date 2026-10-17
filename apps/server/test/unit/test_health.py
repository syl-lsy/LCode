"""unit 层：TestClient 冒烟打 /health（零网络、毫秒级）。

存活探针必须**不依赖任何外部服务**（依赖抖动会误杀健康 Pod）——本用例同时是
「骨架可启动」的最小证据：app 能导入、中间件注册表挂载成功、路由可响应。
"""

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_returns_ok():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_is_outside_api_prefix_so_not_wrapped():
    """/health 在 /api/ 之外：不套成功信封（探针消费方是编排系统，不是客户端契约）。"""
    resp = TestClient(app).get("/health")
    assert set(resp.json().keys()) == {"status"}
