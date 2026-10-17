# LCode — AGENTS.md

## 项目定位

LCode 是**个人开发项目**：ZCode 风格的交互式 Coding Agent，定位是**日常自用的生产力工具**——先做到个人稳定使用、用熟，再考虑部署上线给多人用。按真实工具的质量标准开发，不是一次性学习 demo。

**阶段路线**：
- **P0**：MVP 七件套跑通，个人自用（当前阶段）
- **P1**：用熟迭代，按需解锁二期能力（RAG 三件套/浏览器/自定义工作流等）
- **P2**：多人部署上线——届时重审鉴权/配额/LiteLLM Proxy/多租户隔离（架构已预留接缝，届时另行出方案）

**用户模型**：当前单人自用，数据模型从第一天带 `user_id`；鉴权 P2 之前不实现。

**产品形态**：Web 应用。客户端路线图：Web（主）→ Electron 套壳 Desktop → CLI 轻客户端（可选）→ TUI（三期，届时再定"云端看板 vs 本地执行版"）。

**当前状态**：决策基线冻结；**PRD 已定稿**（`docs/PRD.md`，增量 diff 演进）。仓库已初始化（基线 tag `m1-start`）；**M1 完成**（tag `m1-done`）：`contracts/` 契约冻结 v1（五域 + 109 用例自校验 harness + TS/zod 生成管线，经用户评审通过）；`docs/adr/ADR-001~005` 与 `docs/arch/` 前后端架构详设就位；双端脚手架与根质量门禁工具链全绿（`apps/server` 99 用例 / `web/` 22 用例；GitHub Actions CI 已配置，待首次 push 后验证）。**下一步 M2**：聊天 + 沙箱闭环（REQ-001/002/003/004/014/015）。所有架构决策已冻结为基线，改动需用户确认。

## 技术栈（冻结基线）

- **前端**：React + TypeScript + Vite + TailwindCSS
- **后端**：Python + FastAPI + SQLAlchemy + PostgreSQL + pgvector
- **Agent**：LangGraph（外层自建编排图）+ deepagents（内层执行单元，当库用）
- **执行环境**：百炼 Sandbox（E2B 协议兼容，默认）+ OpenSandbox（自托管备选，阿里开源）双适配器，配置开关一键切换（provider: `bailian | opensandbox`）
- **模型**：OpenAI 兼容网关，provider 可切换（百炼 Qwen 打头）；不部署 LiteLLM

## 架构总纲：分层混合

```
外层自建 LangGraph 编排图（entry / router / plan_mode / fan_out(Send) / reduce）
  └─ 内层 deepagents 实例 = subgraph 执行单元（工具循环/压缩/审批/子智能体内置）
```

- 宏观流程（路由、计划模式、map-reduce 扇出、自定义工作流挂载点）在外层图
- 微观执行（文件工具、压缩、task() 子智能体、审批 interrupt）在内层 deepagents
- 子智能体 MVP 4 个：`general-purpose`（默认）/ `explorer`（只读免审批）/ `coder`（全工具+沙箱）/ `reviewer`（只读+结构化 findings）
- 客户端形态与执行环境是**正交开关**：任何客户端都只消费 SSE 事件协议

## 关键接缝（改代码前必读）

| 接缝 | 规则 |
|---|---|
| `get_llm(role)` | 模型工厂唯一入口；优先级：请求级 > 会话级 > 角色级 > 模型默认。配置在 `config/models.yaml` |
| `SandboxClient` | 沙箱抽象，接口设计**必须先对齐 deepagents `backends/protocol.py` 的方法面**，桥接适配器保持薄翻译；双实现 `bailian`（E2B 协议）/ `opensandbox`（自有协议），provider 由配置开关一键切换（切换粒度在 M1 契约定稿），新增 provider = 新增一个实现类，不改上层 |
| 事件协议 | 所有客户端/面板只消费自定义领域事件（`message_delta / tool_call_* / approval_request / diff_ready / sandbox_output / browser_action`），不透传 LangGraph 原始事件；字段级定义以 `contracts/src/lcode_contracts/events.py` 为准（Q5 定稿，含 run_status 运行状态族） |
| Agent 扩展点 | 唯一机制 = middleware 栈（`create_deep_agent(middleware=[...])`；name 合并/替换语义见快照 `graph.py` 的 `_apply_custom_middleware`）；LCode 业务 middleware 清单与顺序在 M1 ADR「扩展性与 hooks」冻结；P1 hooks = HookBridgeMiddleware 读 `~/.lcode/hooks.json`（REQ-108），插件系统不排期（REQ-109 P2 占位，工具扩展走 MCP） |
| 记忆与持久化 | 双轨（REQ-004 约束）：短期 = LangGraph checkpointer（`AsyncPostgresSaver`，thread_id=会话 ID）**仅运行时断点恢复**，审批 interrupt 依赖它；长期 = REQ-101 三件套（自有表 + pgvector），SDK `StoreBackend` 桥 PostgresStore（namespace 按用户隔离）；**业务事实源 = 自有表**，禁止从 checkpoint 反序列化做展示/审计；`InMemorySaver`/`InMemoryStore` 仅 pytest 用 |

## 硬性约束与已知的坑

1. **`e2b==2.31.0` 版本钉死**（更高版本创建百炼沙箱实例返回 405）；`e2b-code-interpreter==2.8.1`
2. **deepagents 默认栈没有 todo 工具**——planning 需显式装配 LangChain `TodoListMiddleware`
3. **`top_k` 非 OpenAI 标准参数**——走 `extra_body`（model_kwargs）透传，传顶层会被吞
4. **reasoning 模型（qwen3 思考系）忽略/禁用 temperature**——工厂层按 `param_support` 剥离并告警
5. **沙箱内禁用审批 AUTO 模式**（分类器自动放行仅限非沙箱工具）
6. **网页内容进上下文前必须消毒**（strip script/style、截断、标记不可信，防提示注入）
7. **密钥只进环境变量**（`api_key_env` 引用），永不入配置文件/提示词/日志
8. 空闲沙箱成本靠后端**显式调 `pause()`**——模板的空闲超时自动休眠不保留状态
9. thinking 模型的推理 token 计入 output（成本口径）
10. **模版级文件挂载上限 5 个文件**（百炼官方限制）——批量代码载入沙箱走数据面（`files.write` / 打包上传后解压），不靠模版挂载；镜像是否预装 git 官方未列明，需 spike 实测（PRD 开放问题 Q7）；沙箱写文件本身走 `files.write`（`POST /files?path=...`），与 git 无关
11. **钩子单一闸门原则**：钩子（REQ-108 hooks）只能**加限制**（deny/ask/观察/反馈），**永不放行审批**——审批唯一闸门是 permissions + interrupt（REQ-013）；ask 仅把该次工具调用追加进审批策略强制走 interrupt → 人工决定 → resume，只会让 Agent 停（hook 可触发 HITL 但不可消化 HITL）；钩子不自持 interrupt、不另立闸门/resume 契约（HookBridgeMiddleware 在 HITL middleware 之前注入）；dcode 的 hook allow/deny 会抑制审批弹窗，LCode 禁止 hook 侧 allow
12. **OpenSandbox 暂无稳定 v1 API 承诺**（路线图明示；自有协议，非 E2B 兼容）——适配层锁版本（当前 1.1.x 统一版本线），升级必须过适配层回归测试；其 Fast Sandbox（Firecracker）需 Linux/KVM，macOS 本地退化为 Docker 后端

## 工程纪律

- **契约先行**：事件 schema、API 契约、数据模型冻结在 `contracts/` 后才并行开发；改契约同步所有受影响方
- **质量门禁**：测试/lint/类型检查干净是"完成"的必要条件（工具链：ruff + mypy + pytest / eslint + tsc + vitest，仓库初始化时建立）；功能"完成"的行为判据以 `docs/PRD.md` 对应 REQ 的验收标准（AC-xxx-x）为准
- **TDD 开发模式**：前后端一律测试先行——先写失败测试（红）→ 最小实现（绿）→ 重构；bug 修复先写复现测试再修；新功能的完成判据 = 新增测试通过 + 既有测试全绿（与质量门禁工具链 pytest/vitest 配合）
- **每日任务记录**：当天工作与任务总结落盘 `docs/daily/`（一文件一天，`YYYY-MM-DD.md`，规则见该目录 README）。每日 23:00 定时任务（自动化"LCode项目每天的完成任务和工作的总结"）自动汇总生成；会话收尾时如当天文件不存在或有重要遗漏，手动补充。性质是进度日志，需求事实源仍是 `docs/PRD.md`
- **最小方案**：MVP 七件套 = 聊天 + 沙箱执行 + 流式 + 会话持久化 + planning/todo + 工具审批 + diff 可视化。**二期清单（勿提前实现）**：RAG 三件套（长期记忆/代码索引/OpenWiki）、自定义工作流 DSL、浏览器能力、fetch_url 自研、fallback 链、多用户
- UI 不暴露采样参数（temperature/top_p/top_k），只暴露**模型选择器 + reasoning-effort 档位**

## 参考文档

- **`docs/PRD.md`（需求唯一事实源，动手前先读）**：版本以文件头版本表与变更记录为准（增量 diff 演进）；16 项 P0 需求（REQ-001~016）+ P1/P2 编号占位（REQ-101+/REQ-201+），每条 P0 需求带 Given/When/Then 验收标准；含 NFR-001~008、里程碑 M1-M4（实施顺序：M1 契约 → M2 聊天+沙箱 → M3 审批+diff+planning → M4 扇出+Web 工具）、三栏线框、开放问题（均附建议默认值，条目数以文件第 10 节为准；Q5/Q6 事件 schema 与结构化输出 schema 归 architect 在 contracts/ 定稿）。**需求变更走增量 diff（增/改/废 REQ 编号），不整篇重写**；下游 ui-designer（视觉稿/tokens）与 architect（contracts/ 契约）均以它为输入。
- 百炼 Sandbox 文档：`https://docs.bailian.console.aliyun.com/zh/model-studio/sandbox-introduction`（E2B 兼容接入/pause/计费）
- OpenSandbox（自托管沙箱备选底座）：`https://github.com/opensandbox-group/OpenSandbox`（阿里系开源、Apache 2.0、15.9k stars、CNCF landscape；Docker/K8s/Firecracker 三后端；五语言 SDK + CLI + MCP server；**自有协议非 E2B 兼容**，本地模式仍依赖 Docker，Firecracker 需 Linux/KVM；1.1.0（2026-09-21）起统一版本治理；调研结论见全局记忆 2026-10-15 沙箱选型条目）
- deepagents 源码（核心依赖，重点读）：**本地快照 `docs/reference/deepagents-main/`，查 deepagents 源码直接读本地文件，不要联网搜 GitHub**。重点：`docs/reference/deepagents-main/libs/deepagents/deepagents/middleware/`（summarization/filesystem/subagents 等）与 `docs/reference/deepagents-main/libs/deepagents/deepagents/backends/protocol.py`（`SandboxClient` 接缝须对齐的方法面）。读源码前可先看快照根 `openwiki/` 的对应主题导航页（quickstart/architecture/concepts 等，由 openwiki 工具生成、带 sources 溯源；注意 wiki 是验证线索，源码与测试才是权威）
- deepagents-code（dcode，同快照 `docs/reference/deepagents-main/libs/code/`）：**LCode 应用层参考实现，只读不依赖**。可抄设计：HOOKS.md（用户级钩子：12 生命周期事件 + matcher + 三级作用域 + 信任门禁）、EXTENSIONS.md 与 plugins/（插件 manifest：hooks/命令/Python 扩展三贡献面，注册面 register_middleware/register_tool/register_backend_route/on_shutdown）、双进程 client-server + 流式协议、cost_tracking/reasoning_effort/approval_mode（同构 REQ-015/014/013）。**禁止 pip 依赖或 copy 其内部模块**（reference implementation 无稳定 API 承诺）；工具扩展主通道仍是 MCP，插件系统只补 hooks/middleware 两面
- ZCode 动态工作流（`/workflow`，**官方文档未收录**）：作者契约与设计蓝本读本机 `/Applications/ZCode.app/Contents/Resources/glm/packages/bundled-skills/skills/dynamic-workflows/SKILL.md`——REQ-103 的参照实现（TypeScript 脚本编排、类型化子智能体 `agent().ask<T>()`、确定性命令门禁 `world.run()`、`phase()` 阶段图、`hole()` 增长式编排、AmendWorkflow 缓存式修正、report/artifact 交付面）。借鉴设计，不依赖其宿主实现
- LangChain 文档：event-streaming / human-in-the-loop / context-engineering / mcp
- ZCode 产品参照（**文档官网 `https://zcode.z.ai/cn/docs/welcome`**，设计对标先例库，按需查对应页）：`ADE-tools`（侧边四标签 = 前端信息架构蓝图：审查↔diff_ready、终端↔sandbox_output、浏览器↔browser_action、辅助对话↔同 context 兄弟 thread）、`safety-confirm`（审批先例↔REQ-006/013）、`subagents`（↔REQ-016）、`hooks`（↔REQ-108，语义对照 dcode HOOKS.md）、`plugin`（↔REQ-109 P2 占位）、`repo-wiki`（↔REQ-101 OpenWiki 产品先例）、`memory`（↔REQ-101 长期记忆）、`mcp-services`（↔REQ-012 Web 工具接入）、`skill`/`commands`（P1 技能与命令参考）
- **UI 活参照**：本机 `http://127.0.0.1:3102`（zcode-webui 跑官方 ZCode 3.15.3，启动/使用/截图注意见 `docs/reference/zcode-ui/README.md`；含 15 张状态截图与全量 design tokens）——前端/UI 工作动手前先看该目录

## 构建命令

根 `Makefile` 是质量门禁命令的**单一事实源**（CI 只调这些目标，不复制命令）；全部从仓库根执行，`make help` 看全量。

- **开发**：`make dev`（后端 fastapi dev :8080，自举 .env）/ `make dev-web`（Vite，/api 代理 127.0.0.1:8000）/ `make debug`（debugpy :5678）
- **测试三层环**：`make test`（后端 unit+integration，`--cov-fail-under=80`）/ `make test-e2e`（子进程真服务，自举 .env.test）/ `make test-web`（vitest 全量；骨架期不挂前端覆盖率阈值）/ 细分 `make test-unit`、`test-integration`
- **静态检查**：`make lint`（后端 ruff check + format --check + mypy；前端 eslint）/ `make typecheck`（前端 tsc；后端 mypy 已在 lint 内）
- **契约**：`make contracts`（= `make -C contracts check`；`make -C contracts gen` 重新生成产物；TS 侧需 node≥20）
- **CI 聚合**：`make ci` = `ci-backend`（contracts+lint-py+test+test-e2e）+ `ci-frontend`（frozen install+lint-web+typecheck+test-web+build-web）；串行语义，勿加 `-j`
- **开发内环**：`make test-changed`（工作区变更）/ `make test-changed SINCE=m1-start`（自某 ref 起；**SINCE 是 make 变量**，`--since` 不是合法 make 选项；脚本选项形态为 `uv run python scripts/run_changed_tests.py --since <ref>`）——非门禁
- **DB**：`make db-migrate message="..."` / `db-upgrade` / `db-downgrade version=-1`（M2 真用，需 Postgres 可达；docker-compose 起库目标留 M2 TODO）
- **清理**：`make clean`（缓存与构建产物，不动 .venv/node_modules/.env*）
