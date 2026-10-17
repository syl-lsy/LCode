"""integration 层示例：经 ASGITransport 在进程内请求真实 app。

这是模式示例（骨架版不依赖外部服务）；真实集成测试应指向真实外部依赖
（数据库、对象存储等），断言点放在「真实依赖的行为」上，并在缺凭证时用
requires_* 标记按需跳过（见 references/testing.md）。
"""


async def test_health_via_asgi_transport(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
