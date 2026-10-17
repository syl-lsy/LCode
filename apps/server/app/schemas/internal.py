"""服务内 DTO（不出 wire）：契约之外的内部数据结构。

与 `app/schemas/__init__.py` 的分界：**凡是客户端可见的字段都归契约包**
（`lcode_contracts.rest` / `.events`），这里只放服务内部传参/中间结构。
M2 起按需落地，当前登记计划中的成员：

- `Cursor` / `parse_cursor()`：游标分页的水位解析（messages / approvals 列表）；
- `EmitContext`：事件发射入参（session_id / run_id / agent 归属）——由
  `events/emitter.py` 消费，避免 service 层直接拼事件信封；
- `SessionModelCfg`：run 启动时从会话行读出的模型/档位快照（get_llm 的会话级入参）。
"""
