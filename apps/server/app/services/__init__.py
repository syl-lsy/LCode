"""业务逻辑层：按域一文件（A3 §1.1），全部继承 `base.BaseService`。

M2 任务 1/7/8 落地清单（A3 §10）：

- `session_service.py`：会话 CRUD / 归档 / 删除（联动沙箱 kill，AC-002-3）；
- `message_service.py`：用户消息落库 + `message_created` 事件发射 + run 行创建
  （空白判定 MessageEmpty 的**首位前置检查**在本文件）；
- `run_service.py`：run 生命周期（启动 RunExecutor 后台任务 / stop / 状态查询）——
  API 层通往 Agent 的唯一入口；
- `approval_service.py` / `diff_service.py`：审批四决策落库 + resume；基线链与 diff 裁决；
- `cost_service.py`：model_calls 记录 / 三维汇总 / 沙箱费用估算（AC-015）；
- `sandbox_service.py`：会话沙箱编排（创建/载入代码/暂停恢复/终止/重建，AC-002-1~5）。

约定：service 只 raise `BusinessException` 子类（不手拼错误 JSONResponse）；
库操作只 `flush()` 不 `commit()`（事务边界在 `dependencies.get_db`）。
"""
