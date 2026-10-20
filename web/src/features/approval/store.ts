import { create } from 'zustand';

import type { ApprovalResolvedEvent } from '@contracts/events';
import type { ApprovalItem } from '@contracts/rest';

/**
 * approvalStore：挂起审批表 + 决策提交中状态（A4 §3.2 approvalStore 行 / §5.3）。
 *
 * 写入者：dispatcher（approval_request/approval_resolved 事件）与 REST 恢复
 * （GET /approvals?status=pending，AC-006-5 挂起跨刷新）。`deciding` 是本地提交态
 * （幂等去重：提交中禁用按钮，重复决策由后端 APPROVAL_ALREADY_RESOLVED 兜底）。
 */
export type ApprovalStatus = ApprovalItem['status'];
export type ApprovalDecision = NonNullable<ApprovalItem['decision']>;
export type ApprovalScope = NonNullable<ApprovalItem['scope']>;
export type ApprovalRiskLevel = ApprovalItem['risk_level'];
export type ApprovalEditedArgs = NonNullable<ApprovalResolvedEvent['edited_args']>;

/** 审批卡片 UI 形态（事件 + REST 两通道合并后的本地类型，非 wire 形状）。 */
export interface ApprovalEntry {
  readonly id: string;
  readonly sessionId: string;
  readonly tool: string;
  readonly args: Readonly<Record<string, unknown>>;
  readonly riskLevel: ApprovalRiskLevel;
  readonly reason: string | null;
  readonly createdAt: string;
  readonly toolCallId: string | null;
  readonly runId: string | null;
  readonly status: ApprovalStatus;
  readonly decision: ApprovalDecision | null;
  readonly editedArgs: ApprovalEditedArgs | null;
  readonly feedback: string | null;
  readonly decidedBy: string | null;
  readonly scope: ApprovalScope | null;
}

export interface ApprovalStore {
  byId: Map<string, ApprovalEntry>;
  /** 待决策 id（卡片四决策按钮就绪判据），保持稳定顺序 */
  pendingIds: string[];
  /** 本地提交中（防重复提交；提交失败由调用方清除） */
  deciding: Set<string>;
  upsertPending: (entry: Omit<ApprovalEntry, 'status' | 'decision' | 'editedArgs' | 'feedback' | 'decidedBy' | 'scope'>) => void;
  upsertFromRest: (items: readonly ApprovalItem[]) => void;
  applyResolved: (event: {
    approvalId: string;
    decision: ApprovalDecision;
    scope?: ApprovalScope | null;
    editedArgs?: ApprovalEditedArgs | null;
    feedback?: string | null;
    decidedBy: string;
  }) => void;
  setDeciding: (id: string, deciding: boolean) => void;
  reset: () => void;
}

interface ApprovalData {
  byId: Map<string, ApprovalEntry>;
  pendingIds: string[];
  deciding: Set<string>;
}

function createApprovalSlice(): ApprovalData {
  return { byId: new Map(), pendingIds: [], deciding: new Set() };
}

/** decision → 落库后的 status（审批行投影口径，contracts rest.py ApprovalItem.status）。 */
const STATUS_BY_DECISION: Record<ApprovalDecision, ApprovalStatus> = {
  approve: 'approved',
  edit: 'edited',
  reject: 'rejected',
  respond: 'responded',
};

function withEntry(state: ApprovalData, entry: ApprovalEntry): Partial<ApprovalData> {
  const byId = new Map(state.byId);
  byId.set(entry.id, entry);
  const isPending = entry.status === 'pending';
  const had = state.pendingIds.includes(entry.id);
  const pendingIds = isPending
    ? had
      ? state.pendingIds
      : [...state.pendingIds, entry.id]
    : state.pendingIds.filter((id) => id !== entry.id);
  return { byId, pendingIds };
}

export const useApprovalStore = create<ApprovalStore>((set) => ({
  ...createApprovalSlice(),
  upsertPending: (entry) =>
    set((s) =>
      withEntry(s, {
        ...entry,
        status: 'pending',
        decision: null,
        editedArgs: null,
        feedback: null,
        decidedBy: null,
        scope: null,
      }),
    ),
  upsertFromRest: (items) =>
    set((s) => {
      const byId = new Map(s.byId);
      const pending: string[] = [];
      for (const item of items) {
        const existing = byId.get(item.id);
        byId.set(item.id, {
          id: item.id,
          sessionId: item.session_id,
          tool: item.tool,
          args: item.args,
          riskLevel: item.risk_level,
          reason: item.reason ?? null,
          createdAt: item.created_at,
          toolCallId: existing?.toolCallId ?? null,
          runId: existing?.runId ?? null,
          status: item.status,
          decision: item.decision ?? null,
          editedArgs: existing?.editedArgs ?? null,
          feedback: existing?.feedback ?? null,
          decidedBy: existing?.decidedBy ?? null,
          scope: item.scope ?? null,
        });
        if (item.status === 'pending') pending.push(item.id);
      }
      return { byId, pendingIds: pending };
    }),
  applyResolved: ({ approvalId, decision, scope = null, editedArgs = null, feedback = null, decidedBy }) =>
    set((s) => {
      const existing = s.byId.get(approvalId);
      if (!existing) return {}; // 未加载的审批：不凭空造卡片（REST 列表可补齐）
      return withEntry(s, {
        ...existing,
        status: STATUS_BY_DECISION[decision],
        decision,
        editedArgs,
        feedback,
        decidedBy,
        scope,
      });
    }),
  setDeciding: (id, deciding) =>
    set((s) => {
      const next = new Set(s.deciding);
      if (deciding) next.add(id);
      else next.delete(id);
      return { deciding: next };
    }),
  reset: () => set(createApprovalSlice()),
}));
