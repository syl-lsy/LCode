"""e2e 层示例：对真服务进程发请求（经 base_url fixture，服务由 conftest 起停）。"""

import httpx


def test_health_endpoint(base_url):
    resp = httpx.get(f"{base_url}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
