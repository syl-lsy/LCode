"""编排层（ADR-002 分层混合：外层自建 LangGraph 图 + 内层 deepagents 实例）。

M1 骨架只有包壳，M2 任务 3/5/6 按 A3 §5 填肉（文件级计划）：

- `state.py`：外层图 `OuterState`（TypedDict）；
- `graph.py`：`build_graph(checkpointer)` 外层图构建，暴露编译图；
- `nodes/`：`entry.py / router.py / plan_mode.py / fan_out.py / reduce.py`；
- `assembly.py`：`build_inner_agent(session_cfg, backend, approval_mode=...)`——middleware
  栈的**唯一装配点**（A3 §5.2）；M3 起把 `interrupt_on` 交给 create_deep_agent（tail 自动
  装 HITL，子智能体继承同一策略；checkpointer 只挂父图）；
- `subagents.py`：四个预置子智能体规格（general-purpose / explorer / coder / reviewer）；
- `middleware.py`：LCode 业务 middleware 清单与顺序；
- `permissions.py`：REQ-013 权限规则表 → `FilesystemPermission` + `interrupt_on`（MANUAL/AUTO
  两种模式、沙箱红线不变量、`risk_level_for`/`approval_reason`）；
- `llm_factory.py` / `models_registry.py`：`get_llm(role)` 唯一入口 + `config/models.yaml` 加载；
- `runtime.py`：`RunExecutor`（唯一 astream 消费者）；
- `tools/`、`prompts/`：自定义工具（P0 空）与系统提示词模板（.md 文件）。

依赖禁令（A3 §1.2，review checklist）：`agent/` 不得 import `app/events`（框架纯净，
middleware 用 LangGraph `get_stream_writer()` 发自定义流）；`api/` 不得直接 import `agent/`。
"""
