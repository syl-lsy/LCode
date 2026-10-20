"""测试替身支持包（test-plan-m2 §3.1/§3.2）：unit / integration / e2e 三层共用的假件。

- `fake_sandbox.py`：`FakeSandbox(LcodeSandbox)`——本地 tmp 目录 + 真子进程 execute、
  真 tmp 文件读写、真 git、暂停/恢复/终止轨迹与剧本注入（AC-002-1~5）；
- `fake_llm.py`（M2 任务 5/7 落点）：剧本化假模型（`FakeMessagesListChatModel` +
  `ScriptedStreamingChatModel`）。

为什么是包而不是 sys.path 魔法：`apps/server/test/` 是包里目录（`__init__.py` 齐备），
pytest 的 prepend import 模式会把 `apps/server` 插进 sys.path，三层测试统一
`from test.support.fake_sandbox import FakeSandbox` 引用（与 mypy 的 `apps/server`
搜索路径同一口径），不依赖 cwd、不写 sys.path 补丁。
"""
