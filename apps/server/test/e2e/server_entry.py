"""e2e 服务子进程入口：真 app + 按环境开关装配替身/真库 checkpointer，再由 uvicorn 开放端口。

用法（由 test/e2e/conftest.py 启动；也可手动复现）::

    uv run --package lcode-server python apps/server/test/e2e/server_entry.py [PORT]

开关（仅测试车道；生产 .env 不设这些变量 → 本 entry 等价于直接跑 app.main）：

- `LCODE_E2E_FAKE_LLM=1`：ScriptedChatModel 剧本路由假模型（fake_runtime.install_fake_llm）；
- `LCODE_E2E_FAKE_SANDBOX=1`：FakeSandbox（本地 tmp + 真子进程，install_fake_sandbox）；
- `LCODE_E2E_CHECKPOINTER=postgres`：真 AsyncPostgresSaver——**app lifespan 装配**
  （`app/core/postgres_checkpointer.py`；连接绑定事件循环，必须随应用起停）。

装配顺序：开关注入（dependency_overrides / 进程级单例替换）先于 uvicorn 服务——
「开放流量时装配已生效」；uvicorn 运行 lifespan 期间真 checkpointer 建连并 setup。
开关只在装配面读取，不进 wire 契约、不进生产 `.env.example`（test-plan §3.1/§6-5）。
"""

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]  # apps/server
ROOT = Path(__file__).resolve().parents[4]
# 无条件插到最前：uv 会把 workspace 路径经 PYTHONPATH 追加在 stdlib 之后，标准库自带
# 同名 `test` 包（CPython 自测套件）会抢先解析——必须压过它（pytest 的 prepend 模式
# 同理）。重复项无害，不做成员判断。
sys.path.insert(0, str(SERVER_DIR))

from dotenv import load_dotenv  # noqa: E402 —— sys.path 就位、.env.test 先于 app import 装载

load_dotenv(ROOT / ".env.test")

# 顺序钉死：test.e2e 先于 app.main 导入——app 依赖链里若有任何模块触到标准库 `test`
# 包，sys.modules['test'] 会被缓存成 stdlib 版本，此后的 `import test.e2e` 将解析失败
# （实测踩坑：ModuleNotFoundError: No module named 'test.e2e'）。字母序会让 ruff 想把
# test.e2e 挪到 app.main 之后——此处顺序即语义，禁掉排序规则。
from test.e2e import fake_runtime  # noqa: E402, I001

from app.main import app  # noqa: E402


def main() -> None:
    """按开关装配后起服务（端口取 argv[1]，缺省 WEB_PORT/18000）。"""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("WEB_PORT", "18000"))

    if os.environ.get("LCODE_E2E_FAKE_LLM") == "1":
        fake_runtime.install_fake_llm(app)
        print("[e2e] 假模型已装配（ScriptedChatModel 剧本路由）", flush=True)
    if os.environ.get("LCODE_E2E_FAKE_SANDBOX") == "1":
        fake_runtime.install_fake_sandbox()
        sandbox_root = os.environ.get("LCODE_E2E_SANDBOX_ROOT", "")
        print(f"[e2e] 假沙箱已装配（FakeSandbox，根={sandbox_root}）", flush=True)

    import uvicorn  # 装配完成后才起服务（局部导入：装配失败时不留半开端口）

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
