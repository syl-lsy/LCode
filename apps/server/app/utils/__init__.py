"""通用工具（被所有层引用，自身不依赖上层）：可 patch 的时钟与错误分类。

M1 骨架只有包壳，M2 按需填肉（文件级计划）：

- `clock.py`：UTC 时钟与 uuid 生成（**统一出口**，测试固定时间/ID 时 patch 这里）；
- `error_classifier.py`：内部异常 / SDK 异常 → `ToolErrorCode`(9 值) | `RunErrorCode`(5 值)
  + `retryable` 分类（契约事件侧命名空间，不进 REST 的 ERROR_MAP，A3 §8.1）。
"""
