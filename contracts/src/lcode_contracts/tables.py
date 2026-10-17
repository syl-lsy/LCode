"""数据模型契约（列级规格，SQLAlchemy 2.0 + Alembic 直接可落）。

边界（REQ-004 记忆双轨约束）：
- LangGraph checkpoint 三表（checkpoints / checkpoint_writes / checkpoint_blobs）
  由 `langgraph-checkpoint-postgres` 的 AsyncPostgresSaver 自管，**不在本契约内**；
  其唯一用途是 agent 图运行时断点恢复（审批 interrupt），禁止从中反序列化做
  展示/审计；
- 业务事实源 = 本契约的自有表；events 表是界面重建（AC-008-3）的唯一回放来源；
- 全表带 user_id（NFR-008，P0 固定单用户值 'local'）；P2 前不建 users 表。

数据量级约束（个人自用）：单会话事件约 10^3~10^4 行、年量级 10^6~10^7 行 ——
events 表按 (session_id, seq) 唯一索引即可覆盖重放路径；不分区、不预建归档
策略（P2 再议）。model_calls 聚合走 (user_id, created_at) 索引即可。

用法：apps/server 的 conformance 测试逐表断言 SQLAlchemy metadata 与本规格一致
（列名/类型/可空/默认/唯一/外键），迁移漂移在 CI 阶段就被拦下（挂点见
contracts/README.md「阶段 2 接入」）。
"""

from typing import Literal

from pydantic import Field

from .common import ContractModel

PgType = Literal[
    "uuid", "text", "varchar", "integer", "bigint", "boolean",
    "timestamptz", "jsonb", "numeric",
]


class ColumnSpec(ContractModel):
    name: str
    type: PgType
    nullable: bool = False
    primary_key: bool = False
    unique: bool = False
    default: str | None = None  # SQL 字面量/表达式，如 "'local'" / "gen_random_uuid()" / "now()"
    references: str | None = None  # "表.列"
    enum_values: list[str] | None = None  # varchar 列的 CHECK 值域（非空列表才生效）

    @staticmethod
    def uuid_pk() -> "ColumnSpec":
        return ColumnSpec(name="id", type="uuid", primary_key=True, default="gen_random_uuid()")

    @staticmethod
    def user_id() -> "ColumnSpec":
        return ColumnSpec(name="user_id", type="text", default="'local'")

    @staticmethod
    def created_at() -> "ColumnSpec":
        return ColumnSpec(name="created_at", type="timestamptz", default="now()")

    @staticmethod
    def fk(name: str, ref: str, nullable: bool = False) -> "ColumnSpec":
        return ColumnSpec(name=name, type="uuid", nullable=nullable, references=ref)


class IndexSpec(ContractModel):
    name: str
    columns: list[str] = Field(min_length=1)
    unique: bool = False


class TableSpec(ContractModel):
    name: str
    description: str
    columns: list[ColumnSpec] = Field(min_length=1)
    indexes: list[IndexSpec] = Field(default_factory=list)


def _t(name: str, description: str, columns: list[ColumnSpec], indexes: list[IndexSpec]) -> TableSpec:
    return TableSpec(name=name, description=description,
                     columns=[ColumnSpec.uuid_pk(), ColumnSpec.user_id(), *columns,
                              ColumnSpec.created_at()],
                     indexes=indexes)


TABLES: list[TableSpec] = [
    _t(
        "sessions",
        "会话（REQ-004/014）：标题/模型/档位/审批模式/沙箱 provider 均为会话级可变更项",
        [
            ColumnSpec(name="title", type="text", default="'新会话'"),
            ColumnSpec(name="status", type="varchar", enum_values=["active", "archived"], default="'active'"),
            ColumnSpec(name="model", type="text"),
            ColumnSpec(name="reasoning_effort", type="varchar", enum_values=["off", "low", "medium", "high"]),
            ColumnSpec(name="approval_mode", type="varchar", enum_values=["manual", "auto"], default="'manual'"),
            ColumnSpec(name="sandbox_provider", type="varchar", enum_values=["bailian", "opensandbox"]),
            ColumnSpec(name="updated_at", type="timestamptz", default="now()"),
            ColumnSpec(name="last_active_at", type="timestamptz", default="now()"),
        ],
        [IndexSpec(name="ix_sessions_user_active", columns=["user_id", "last_active_at"])],
    ),
    _t(
        "runs",
        "一次用户消息触发的 agent 运行（REQ-001/003）：waiting_approval = 审批挂起中",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec(name="status", type="varchar",
                       enum_values=["running", "waiting_approval", "completed", "stopped", "error"]),
            ColumnSpec(name="error_code", type="varchar", nullable=True),
            ColumnSpec(name="error_message", type="text", nullable=True),
            ColumnSpec(name="started_at", type="timestamptz", default="now()"),
            ColumnSpec(name="finished_at", type="timestamptz", nullable=True),
        ],
        [IndexSpec(name="ix_runs_session_started", columns=["session_id", "started_at"])],
    ),
    _t(
        "messages",
        "消息（REQ-001/004）：agent_id 非空表示子智能体消息；structured 为 REQ-011 载荷",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="role", type="varchar", enum_values=["user", "assistant"]),
            ColumnSpec(name="agent_id", type="text", nullable=True),
            ColumnSpec(name="content", type="text", default="''"),
            ColumnSpec(name="thinking", type="text", nullable=True),
            ColumnSpec(name="finish_reason", type="varchar", nullable=True,
                       enum_values=["stop", "length", "content_filter", "error", "cancelled"]),
            ColumnSpec(name="structured", type="jsonb", nullable=True),
        ],
        [
            IndexSpec(name="ix_messages_session_created", columns=["session_id", "created_at"]),
            IndexSpec(name="ix_messages_session_run", columns=["session_id", "run_id"]),
        ],
    ),
    _t(
        "tool_calls",
        "工具调用（REQ-003/008）：tool_call_id 为 wire 层调用标识（LangGraph/OpenAI 口径）",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="agent_id", type="text", nullable=True),
            ColumnSpec(name="tool_call_id", type="text"),
            ColumnSpec(name="tool", type="text"),
            ColumnSpec(name="args", type="jsonb", default="'{}'"),
            ColumnSpec(name="status", type="varchar", enum_values=["running", "success", "error"]),
            ColumnSpec(name="error_code", type="varchar", nullable=True),
            ColumnSpec(name="error_message", type="text", nullable=True),
            ColumnSpec(name="exit_code", type="integer", nullable=True),
            ColumnSpec(name="duration_ms", type="integer", nullable=True),
            ColumnSpec(name="result_preview", type="text", nullable=True),
            ColumnSpec(name="truncated", type="boolean", default="false"),
            ColumnSpec(name="finished_at", type="timestamptz", nullable=True),
        ],
        [
            IndexSpec(name="ix_tool_calls_session_created", columns=["session_id", "created_at"]),
            IndexSpec(name="ix_tool_calls_run", columns=["run_id"]),
            IndexSpec(name="ix_tool_calls_call_id", columns=["tool_call_id"]),
        ],
    ),
    _t(
        "approvals",
        "审批记录（REQ-006/013）：决策人/时间/决策留痕（AC-006-2）；pending 为挂起态（AC-006-5）",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="tool_call_id", type="text"),
            ColumnSpec(name="tool", type="text"),
            ColumnSpec(name="args", type="jsonb"),
            ColumnSpec(name="risk_level", type="varchar", enum_values=["medium", "high"]),
            ColumnSpec(name="reason", type="text", nullable=True),
            ColumnSpec(name="status", type="varchar", default="'pending'",
                       enum_values=["pending", "approved", "rejected", "edited", "responded"]),
            ColumnSpec(name="decision", type="varchar", nullable=True,
                       enum_values=["approve", "edit", "reject", "respond"]),
            ColumnSpec(name="scope", type="varchar", enum_values=["once", "session"], default="'once'"),
            ColumnSpec(name="edited_args", type="jsonb", nullable=True),
            ColumnSpec(name="feedback", type="text", nullable=True),
            ColumnSpec(name="decided_by", type="text", nullable=True),
            ColumnSpec(name="decided_at", type="timestamptz", nullable=True),
        ],
        [
            IndexSpec(name="ix_approvals_session_status", columns=["session_id", "status"]),
            IndexSpec(name="ix_approvals_user_status", columns=["user_id", "status"]),
        ],
    ),
    _t(
        "diff_reviews",
        "diff 审查轮次（REQ-007）：round 与基线链一一对应；(session_id, round) 唯一",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="round", type="integer"),
            ColumnSpec(name="verdict", type="varchar", default="'pending'",
                       enum_values=["pending", "accepted", "rejected"]),
            ColumnSpec(name="baseline_kind", type="varchar", enum_values=["head", "init", "accepted"]),
            ColumnSpec(name="baseline_ref", type="text"),
            ColumnSpec(name="baseline_commit", type="varchar"),
            ColumnSpec(name="files", type="jsonb", default="'[]'"),
            ColumnSpec(name="additions_total", type="integer", default="0"),
            ColumnSpec(name="deletions_total", type="integer", default="0"),
            ColumnSpec(name="feedback", type="text", nullable=True),
            ColumnSpec(name="decided_by", type="text", nullable=True),
            ColumnSpec(name="resolved_at", type="timestamptz", nullable=True),
        ],
        [IndexSpec(name="uq_diff_reviews_session_round", columns=["session_id", "round"], unique=True)],
    ),
    _t(
        "git_baselines",
        "git 基线链（REQ-007 / 契约域 5）：round=0 为起点（head 或 init），accepted 逐轮追加",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec(name="round", type="integer"),
            ColumnSpec(name="kind", type="varchar", enum_values=["head", "init", "accepted"]),
            ColumnSpec(name="ref", type="text"),
            ColumnSpec(name="commit", type="varchar"),
            ColumnSpec(name="note", type="text", nullable=True),
        ],
        [IndexSpec(name="uq_git_baselines_session_round", columns=["session_id", "round"], unique=True)],
    ),
    _t(
        "model_calls",
        "模型调用成本（REQ-015 / NFR-003）：思考 token 计入 output_tokens 口径；100% 落库",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="agent_id", type="text", nullable=True),
            ColumnSpec(name="role", type="text", nullable=True),
            ColumnSpec(name="model", type="text"),
            ColumnSpec(name="input_tokens", type="integer", default="0"),
            ColumnSpec(name="output_tokens", type="integer", default="0"),
            ColumnSpec(name="total_tokens", type="integer", default="0"),
            ColumnSpec(name="latency_ms", type="integer", nullable=True),
            ColumnSpec(name="cost", type="numeric", default="0"),
            ColumnSpec(name="currency", type="varchar", default="'CNY'"),
            ColumnSpec(name="status", type="varchar", enum_values=["success", "error"]),
            ColumnSpec(name="error_type", type="varchar", nullable=True),
            ColumnSpec(name="retry_count", type="integer", default="0"),
        ],
        [
            IndexSpec(name="ix_model_calls_session_created", columns=["session_id", "created_at"]),
            IndexSpec(name="ix_model_calls_user_created", columns=["user_id", "created_at"]),
            IndexSpec(name="ix_model_calls_model", columns=["model"]),
        ],
    ),
    _t(
        "sandbox_usage",
        "沙箱计费分段（REQ-015 / AC-015-4）：每个 running/paused 相位段一行，费用=时长×单价",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec(name="provider", type="varchar", enum_values=["bailian", "opensandbox"]),
            ColumnSpec(name="sandbox_id", type="text"),
            ColumnSpec(name="phase", type="varchar", enum_values=["running", "paused"]),
            ColumnSpec(name="started_at", type="timestamptz"),
            ColumnSpec(name="ended_at", type="timestamptz", nullable=True),
            ColumnSpec(name="duration_seconds", type="integer", nullable=True),
            ColumnSpec(name="estimated_cost", type="numeric", nullable=True),
            ColumnSpec(name="currency", type="varchar", default="'CNY'"),
        ],
        [
            IndexSpec(name="ix_sandbox_usage_session_started", columns=["session_id", "started_at"]),
            IndexSpec(name="ix_sandbox_usage_user_started", columns=["user_id", "started_at"]),
        ],
    ),
    _t(
        "events",
        "领域事件库（REQ-008 / AC-008-3）：界面重建唯一回放来源；(session_id, seq) 唯一承载顺序与续传",
        [
            ColumnSpec.fk("session_id", "sessions.id"),
            ColumnSpec(name="seq", type="bigint"),
            ColumnSpec.fk("run_id", "runs.id", nullable=True),
            ColumnSpec(name="agent", type="jsonb", nullable=True),  # AgentRef 快照；null=主智能体
            ColumnSpec(name="type", type="varchar"),
            ColumnSpec(name="data", type="jsonb"),  # 事件除信封字段外的载荷（扁平事件去掉信封列）
        ],
        [
            IndexSpec(name="uq_events_session_seq", columns=["session_id", "seq"], unique=True),
            IndexSpec(name="ix_events_session_run", columns=["session_id", "run_id"]),
        ],
    ),
]

TABLES_BY_NAME: dict[str, TableSpec] = {t.name: t for t in TABLES}


def check_invariants(tables: list[TableSpec] | None = None) -> list[str]:
    """机器可校验的表规格不变量；返回问题清单（空 = 通过）。

    harness 用它证明规格自洽；apps/server conformance 测试复用它 + 对照 ORM。
    """
    tables = tables if tables is not None else TABLES
    by_name = {t.name: t for t in tables}
    problems: list[str] = []
    for t in tables:
        cols = {c.name: c for c in t.columns}
        pks = [c for c in t.columns if c.primary_key]
        if len(pks) != 1 or pks[0].name != "id" or pks[0].type != "uuid":
            problems.append(f"{t.name}: 必须且仅须 id uuid 主键")
        uid = cols.get("user_id")
        if uid is None:
            problems.append(f"{t.name}: 缺 user_id（NFR-008）")
        elif uid.nullable or uid.default != "'local'":
            problems.append(f"{t.name}: user_id 须 NOT NULL DEFAULT 'local'")
        if "created_at" not in cols or cols["created_at"].type != "timestamptz":
            problems.append(f"{t.name}: 缺 created_at timestamptz")
        for c in t.columns:
            if c.references:
                ref_table, _, ref_col = c.references.partition(".")
                if ref_table not in by_name:
                    problems.append(f"{t.name}.{c.name}: 外键目标表 {ref_table} 不存在")
                elif ref_col not in {rc.name for rc in by_name[ref_table].columns}:
                    problems.append(f"{t.name}.{c.name}: 外键目标列 {c.references} 不存在")
            if c.enum_values is not None and not c.enum_values:
                problems.append(f"{t.name}.{c.name}: enum_values 提供时须非空")
        for idx in t.indexes:
            for col in idx.columns:
                if col not in cols:
                    problems.append(f"{t.name}: 索引 {idx.name} 引用不存在的列 {col}")
    events = by_name.get("events")
    if events is not None:
        has_seq_uniq = any(
            i.unique and i.columns == ["session_id", "seq"] for i in events.indexes
        )
        if not has_seq_uniq:
            problems.append("events: 缺 (session_id, seq) 唯一索引（续传与顺序的物理承载）")
    return problems
