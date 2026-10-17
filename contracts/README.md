# LCode contracts/ — 契约单源（M1 冻结 v1）

> 状态：**待用户评审冻结**（M1 确认点②）。冻结后改动走增量 diff（见下「版本演进」）。
> 单源 = 本目录的 pydantic v2 模型；TS 类型与 zod schema 全部生成，禁止手写。

## 一、五域清单

| 域 | 单源文件 | 内容 | 回应需求 |
|---|---|---|---|
| 1. 事件协议 | `src/lcode_contracts/events.py`（+`common.py`/`errors.py`） | 六类领域事件族 + run_status 运行状态族；信封字段、每型字段、错误码、跨字段校验 | REQ-008、Q5 定稿 |
| 2. REST + SSE API | `src/lcode_contracts/rest.py` | 19 端点注册表（ENDPOINTS）+ 23 个 DTO + 查询参数规格（QueryParamSpec）；SSE 帧格式/断线续传语义写在模块 docstring 与本文 | REQ-001/002/004/006/007/014/015 |
| 3. 数据模型 | `src/lcode_contracts/tables.py` | 10 张表列级规格（列名/类型/可空/默认/枚举值域/索引/外键），SQLAlchemy 2.0 + Alembic 直接可落；`check_invariants()` 机器校验 | REQ-004/015、NFR-008 |
| 4. 结构化输出 | `src/lcode_contracts/findings.py` | ReviewerFindings（summary + findings[] + coverage），LangChain structured output 直用 | REQ-011、Q6 定稿 |
| 5. git 基线约定 | `src/lcode_contracts/baseline.py` | `[lcode-baseline]` tag/提交消息规范与 diff 链语义（round-0 起点 → accepted 逐轮推进） | REQ-007、AC-002-5 |

错误码注册表（`errors.py`）：ToolErrorCode（9 值，吸收 tool-fault-tolerance 调研 §4.3）、
RunErrorCode（5 值）、RestErrorCode（13 值）+ HTTP 映射。

表规格补充语义（评审修正冻结，机器断言见 `tests/test_tables.py`）：

- `approvals.status` 含 **`auto_approved`**：AUTO 模式（REQ-013，Must）下分类器每次
  决策落一行 approvals（`status=auto_approved`、`decided_by='classifier'`、
  `reason=分类器理由`、`decided_at` 记时）——这是 AC-013-3「决策留痕」与 PRD 10.2
  风险表「AUTO 决策全量留痕可审计」的**唯一审计面**（PRD 回填由 PM 增量 diff 处理）；
- approvals 全状态枚举：`pending / approved / rejected / edited / responded / auto_approved`
  （pending=挂起态；auto_approved 仅由分类器路径写入，不经人工决策端点）。

### Q5 定稿摘要（供 PRD 增量 diff 回填）

1. **信封扁平**：每事件 = `{seq, session_id, run_id?, agent?, created_at, type, ...载荷}`；
   `agent` 为 null 表示主智能体，`AgentRef{agent_id, role, parent_agent_id,
   spawned_by_tool_call_id}` 承载子智能体归属（task() 卡片 ↔ 进度面板关联键）。
2. **族与细分**：message（created/delta/finished）、tool_call（started/finished，
   P0 不设 delta——参数执行前完整到达，中间输出走 sandbox_output）、approval
   （request/resolved）、diff（ready/resolved）、sandbox_output（channel:
   stdout/stderr/lifecycle）、browser_action（P1 占位，P0 服务端不得产出）。
3. **新增第七族 run_status**（subject: run|compact）：停止/错误/压缩提示无法从六族
   可靠推导（AC-001-2/010-1、NFR-006）——建议 PRD AC-008-1 的枚举补 `run_status`。
4. **顺序与续传**：seq 会话内单调递增（事务回滚可致罕见空洞，只保序不保密度）；
   SSE `id: seq`，`Last-Event-ID` > `?after_seq=` 优先；先落库后广播，重放无缝。
5. **todo/findings 不另立事件**：todo 面板 = 最近一次 `write_todos` 的
   tool_call_finished.todos 快照；reviewer findings = message_finished.structured。
6. **工具容错字段**（调研 §4 落地）：tool_call_finished 带
   `{status, error{code,message,retryable}, truncated, duration_ms, exit_code, result_ref}`；
   diff_ready 总量与逐文件求和一致是强校验（诚实性 F15）。

### Q6 定稿摘要

`ReviewerFindings{schema_version:"1", summary, findings[Finding], coverage?}`；
Finding 含 file/line/end_line（1 起始，文件级为 null）/severity(5 值)/category(5 值)/
title/description/evidence?/suggestion?/confidence。失败路径：重试 1 次 →
`structured.status="fallback"`、data=null，前端标注「未结构化」（AC-011-3 / Q2）。

## 二、单源与双端生成管线

```
src/lcode_contracts/*.py（pydantic v2 —— 唯一改这里）
    │ make -C contracts gen
    ▼
generated/json/**.json（JSON Schema，入库）
    │ tools/ts/gen_ts.mjs（json-schema-to-typescript + json-schema-to-zod，需 node）
    ▼
generated/ts/**.types.ts + **.zod.ts + index.ts（入库；web/ 阶段 2 引入）
```

- 后端消费：apps/server 以 path dependency 引入本包（`tool.uv.sources` 或
  workspace member，ADR-003），事件翻译层出口 = `TypeAdapter(Event)` 校验；
- 前端消费：web/ 引入 `generated/ts`（zod 运行时校验 SSE/REST 载荷）；
- **改契约的正确姿势**：改 pydantic 模型 → `make -C contracts gen` → 提交单源 +
  生成物 + （如涉及）fixtures；`make -C contracts check` 红着就不许合。

## 三、冻结语义与版本演进

- 本目录冻结后，**任何字段变更 = 契约变更**（NFR-002）：需同步 PRD 增量 diff、
  双端消费方、`docs/adr/` 记录（大变更出 ADR，小变更记 ADR 附录）；
- 版本锚点：`schema_version` 字段只在载荷内嵌结构（reviewer_findings v1）；
  事件协议整体版本 = 包版本（pyproject `0.1.0`，破坏性变更升 minor 并在
  `EVENT_TYPES`/`ENDPOINTS` 变更时于提交信息中标注 `[contracts-diff]`）；
- 兼容规则：新增可选字段 = 兼容（生成物重新生成即可）；改语义/删字段/收紧必填 =
  破坏性，需用户确认（与决策基线冻结同级）。

## 四、自校验（契约的 TDD 等价物）

仓库根一条命令：

```bash
make -C contracts check     # = uv run pytest tests -q + bash tools/ts-check.sh
```

红→绿交付流程（首次）：

```bash
make -C contracts check     # 红：generated/json 缺失（test_generated_json_schema_in_sync 失败并给出指引）
make -C contracts gen       # 绿：生成 generated/json（+ node 可用时 generated/ts）
make -C contracts check     # 绿：全部通过
```

harness 覆盖（`tests/`）：

- **正例/负例 fixtures**（`tests/fixtures/`）：事件 15 正 + 24 负（缺必填/类型错/
  枚举外/未知字段/未知 type/跨字段矛盾）、REST 5 正 + 4 负、findings 1 正 + 4 负；
  负例被拒是硬断言（`test_invalid_*_rejected`）；
- **注册表完整性**：每事件 type 必有 canonical fixture；端点引用的 DTO 必在
  REST_DTOS；查询参数规格齐备（名/型/枚举值域）；错误码 ↔ HTTP 映射全覆盖；
- **数据模型不变量**：全表 user_id/PK/created_at/外键/索引可校验 + 负例可检出；
- **生成物同步**：generated/json 与单源逐键一致（防手改、防过期）；generated/ts
  生成后经 git in-sync 守门（工作区必须与版本库一致）；
- **fixtures ↔ zod 实测**（`tools/ts/validate_fixtures.mjs`，node 侧）：全部 fixtures
  过 zod——valid 必接受、invalid 必拒绝，少数豁免仅限 JSON Schema 不可表达的
  跨字段规则（逐条列明，反向守"清单过期"），见「两侧校验强度差异」。

### TODO-TS

- `tools/ts-check.sh` 探测 node：**不可用即显式失败**（不静默跳过）；可用则
  `npm install`（首次，需网络）→ 重新生成 TS → fixture↔zod 实测
  （`validate_fixtures.mjs`）→ `tsc --noEmit` → generated/ts git in-sync 守门；
- git in-sync 守门语义：生成后 `git diff --exit-code -- generated/ts` 必须为空、且
  无未入库新文件——本地"已生成未提交"窗口内红是**预期**（生成物与提交必须同步），
  暂存/提交生成物后绿；无 git 环境显式提示跳过（不许静默）；
- 未尽事项（阶段 2 处理）：① 根 `make ci` 挂 `make -C contracts check`；
  ② web/ 以包/别名引入 `contracts/generated/ts`；③ zod 主版本升级评估
  （当前锁 ^3.23 —— 与 json-schema-to-zod 生成代码的 API 面对齐）。

### 两侧校验强度差异（实测口径，随评审修正更新）

Python 侧（pydantic）仍是超集，但经生成管线修正后差距收窄到**仅跨字段
model_validator**（JSON Schema 无法表达；逐条清单 = `tools/ts/validate_fixtures.mjs`
的 INVALID_EXEMPTIONS，随 fixture 增删同步守门）：

- zod 侧**生效**（生成前做 `$defs` 深度内联解引用，见 `tools/ts/gen_ts.mjs`）：
  required / enum / 数值边界 / minItems / additionalProperties(strict) / pattern
  —— 含全部嵌套 `$ref` 结构（AgentRef/Usage/ToolErrorObj/BaselineRef 等）与
  `EventUnionSchema` 判别联合（12 型），实测面 = fixtures↔zod 校验脚本；
- zod 侧**不生效**（唯一豁免类）：model_validator 跨字段规则——status↔error、
  channel↔phase、totals 求和、edit↔edited_args、subject×phase 载荷耦合、
  strip 空白判定、至少一字段等（16 条事件/REST/findings 负例豁免，逐条注明出处）；
- 立场不变：前端不得依赖 TS 校验兜住语义不变量，服务端出口校验
  （`TypeAdapter(Event)`）是唯一闸门。

## 五、阶段 2 接入挂点

| 挂点 | 动作 |
|---|---|
| 根 Makefile `ci` 目标 | 追加 `make -C contracts check` |
| 根 workspace | `members = ["contracts", "apps/server"]`（contracts 保留独立 pyproject；web 为 npm 不管） |
| apps/server | path dependency 引入 lcode-contracts；conformance 测试：SQLAlchemy metadata 对照 `TABLES` 规格逐表断言；SSE 出口事件先过 `TypeAdapter(Event)` |
| web | 引入 `generated/ts`；SSE/REST 载荷进 zod 校验后再进 store |

## 六、REST 错误码 → HTTP 映射（errors.py 机器可校验版）

| code | HTTP | code | HTTP |
|---|---|---|---|
| INVALID_ARGUMENT | 422 | APPROVAL_NOT_FOUND | 404 |
| MESSAGE_EMPTY | 422 | APPROVAL_ALREADY_RESOLVED | 409 |
| SESSION_NOT_FOUND | 404 | DIFF_REVIEW_NOT_FOUND | 404 |
| RUN_NOT_FOUND | 404 | DIFF_ALREADY_RESOLVED | 409 |
| RUN_NOT_ACTIVE | 409 | MODEL_NOT_FOUND | 404 |
| RUN_ALREADY_ACTIVE | 409 | NOT_FOUND | 404 |
| INTERNAL | 500 | | |

成功信封：`{"code":"0","message":"ok"|null,"data":<DTO>}`（/api/ 前缀，204 与 SSE 不包装）。

## 七、SSE 帧格式示例

```
retry: 3000

id: 118
event: message_delta
data: {"seq":118,"session_id":"0192f0a1-...","run_id":"0192f0a1-...","agent":null,"created_at":"2026-10-17T09:30:05.654321+00:00","type":"message_delta","message_id":"0192f0a1-...","role":"assistant","kind":"text","delta":"已定位为…"}

: keep-alive
```

- `event:` = 事件 type（自定义事件名）；`type` 同时冗余在 data 内（onmessage 直读）；
- 心跳为注释行，不产生事件；断线重连由 EventSource 自动携带 Last-Event-ID。
