# LCode

ZCode 风格的交互式 Coding Agent，个人自用生产力工具——聊天驱动、沙箱内真实执行、全程流式可见、危险操作人工审批、改动 diff 审查后落地。

## 技术栈

- **前端**：React + TypeScript + Vite + TailwindCSS
- **后端**：Python + FastAPI + SQLAlchemy + PostgreSQL + pgvector
- **Agent**：LangGraph（外层编排图）+ deepagents（内层执行单元）
- **执行环境**：百炼 Sandbox（默认，E2B 协议兼容）+ OpenSandbox（自托管备选）双适配器，配置开关一键切换

## 当前状态

**M1 完成**（tag `m1-done`）：`contracts/` 契约冻结 v1（五域 + 109 用例自校验 harness + TS/zod 生成管线，经用户评审通过）；双端脚手架就位（`apps/server` 99 用例 / `web/` 22 用例）；根质量门禁全绿——`make ci`（后端契约 + lint + 测试与覆盖率门禁 + e2e，前端 frozen install + lint + tsc + vitest + build）。

**M2 完成**（tag `m2-done`，基线提交 `32c3ad1`）：十表 ORM 与迁移、沙箱双适配器（百炼 / OpenSandbox）、deepagents 装配、SSE 端点与事件协议、成本追踪、聊天与终端 UI。

**M3 完成**（tag `m3-done`，提交 `831b5dd` 已在 `main` 并推送，CI backend + frontend 双 job 绿）：审批（HITL 四决策与 resume）、AUTO 分类器与沙箱红线、diff 基线链与审查面板、todo / 子智能体 / compact 事件、场景 A 全链路 e2e 与「0 绕审」不变量。23 条验收标准达成 19 / 部分 4 / 未做 0（部分项为 SDK 能力或产品口径取舍，见收口台账 `docs/arch/m3-closure-checklist.md`）。

**下一步 M4**：扇出（map-reduce）+ Web 工具接入；入口条件见收口台账 §8（先统一并行扇出下的用量对账基准）。

## 目录导览

- `apps/server/` — 后端服务（FastAPI，`app/` 分层：api / schema / model / services / exception）
- `web/` — 前端应用（React + Vite，通过 `@contracts` 别名消费契约生成物）
- `contracts/` — 契约单源（pydantic v2 模型 + 生成管线，五域）
- `docs/` — [`PRD.md`](docs/PRD.md) 需求事实源 / `adr/` 架构决策 / `arch/` 前后端详设与测试计划 / `daily/` 每日进度 / `reference/` 参考快照
- `design/prototype/` — 界面设计稿与 design tokens
- `Makefile` — 质量门禁命令单一事实源（CI 只调 make 目标）
- [`AGENTS.md`](AGENTS.md) — 工程指令与决策基线

## 构建命令

根 `Makefile` 为单一事实源（CI 只调 make 目标），全部从仓库根执行，`make help` 看全量：

- 开发：`make dev` / `make dev-web` / `make debug`
- 测试：`make test`（unit + integration + 覆盖率门禁）/ `make test-unit` / `make test-integration` / `make test-e2e` / `make test-web`
- 检查：`make lint` / `make typecheck` / `make contracts`
- 前端构建：`make install-web`（frozen install）/ `make build-web`
- 里程碑全量：`make ci`（= `ci-backend` + `ci-frontend`）
- 开发内环：`make test-changed [SINCE=<ref>]`（非门禁）
- 数据库：`make db-migrate message="..."` / `make db-upgrade` / `make db-downgrade version=-1`（M2 真用）
- 清理：`make clean`

详见 `AGENTS.md`「构建命令」与 `docs/research/github-actions-ci.md`。
