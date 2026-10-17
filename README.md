# LCode

ZCode 风格的交互式 Coding Agent，个人自用生产力工具——聊天驱动、沙箱内真实执行、全程流式可见、危险操作人工审批、改动 diff 审查后落地。

## 技术栈

- **前端**：React + TypeScript + Vite + TailwindCSS
- **后端**：Python + FastAPI + SQLAlchemy + PostgreSQL + pgvector
- **Agent**：LangGraph（外层编排图）+ deepagents（内层执行单元）
- **执行环境**：百炼 Sandbox（默认，E2B 协议兼容）+ OpenSandbox（自托管备选）双适配器，配置开关一键切换

## 当前阶段

M1：仓库初始化 + `contracts/` 契约冻结 + 质量门禁工具链就位。

## 文档入口

- [`docs/PRD.md`](docs/PRD.md) — 需求唯一事实源
- [`AGENTS.md`](AGENTS.md) — 工程指令与决策基线
- [`design/prototype/`](design/prototype/) — 界面设计稿（R3）
- [`docs/reference/`](docs/reference/) — 参考快照（deepagents 源码等）

## 构建命令

（M1 阶段 2 工具链就位后补充）
