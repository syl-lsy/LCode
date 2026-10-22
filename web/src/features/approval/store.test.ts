/**
 * approvalStore 单测（A4 §3.2 approvalStore 行；AC-006-5 / REQ-006）。
 *
 * 重点：pendingIds 与 byId 状态联动；REST 恢复合并（保留事件侧补充字段）；决策 →
 * 状态映射（approve/edit/reject/respond）；deciding 提交态；缺前态不凭空造卡片。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { useApprovalStore } from './store';
import type { ApprovalItem } from '@contracts/rest';

const PENDING_BASE = {
  id: 'ap-1',
  sessionId: 's1',
  tool: 'execute',
  args: { command: 'rm -rf build/' },
  riskLevel: 'high' as const,
  reason: '命中危险命令规则：rm -rf',
  createdAt: '2026-10-17T09:31:00+00:00',
  toolCallId: 'call_j1k2l3',
  runId: 'run-1',
};

function approvalItem(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    id: 'ap-1',
    session_id: 's1',
    tool: 'execute',
    args: { command: 'rm -rf build/' },
    risk_level: 'high',
    status: 'pending',
    reason: '命中危险命令规则：rm -rf',
    created_at: '2026-10-17T09:31:00+00:00',
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalStore.getState().reset();
});

describe('approvalStore：pending 生命周期', () => {
  it('upsertPending → byId + pendingIds；applyResolved → 终态并移出 pending', () => {
    useApprovalStore.getState().upsertPending(PENDING_BASE);

    const store = useApprovalStore.getState();
    expect(store.byId.get('ap-1')).toMatchObject({ ...PENDING_BASE, status: 'pending' });
    expect(store.pendingIds).toEqual(['ap-1']);

    useApprovalStore.getState().applyResolved({
      approvalId: 'ap-1',
      decision: 'edit',
      scope: 'once',
      editedArgs: { command: 'rm -rf dist/' },
      feedback: null,
      decidedBy: 'local',
    });

    const resolved = useApprovalStore.getState();
    expect(resolved.byId.get('ap-1')).toMatchObject({
      status: 'edited',
      decision: 'edit',
      editedArgs: { command: 'rm -rf dist/' },
      decidedBy: 'local',
      scope: 'once',
    });
    expect(resolved.pendingIds).toEqual([]);
  });

  it('四种决策 → 对应落库状态（approve/edit/reject/respond）', () => {
    const expectations = [
      ['approve', 'approved'],
      ['edit', 'edited'],
      ['reject', 'rejected'],
      ['respond', 'responded'],
    ] as const;
    for (const [decision, status] of expectations) {
      useApprovalStore.getState().upsertPending({ ...PENDING_BASE, id: `ap-${decision}` });
      useApprovalStore.getState().applyResolved({
        approvalId: `ap-${decision}`,
        decision,
        decidedBy: 'local',
      });
      expect(useApprovalStore.getState().byId.get(`ap-${decision}`)?.status).toBe(status);
    }
  });

  it('缺前态的 resolved 不凭空造卡片（REST 列表可补齐）', () => {
    useApprovalStore.getState().applyResolved({
      approvalId: 'ghost',
      decision: 'approve',
      decidedBy: 'local',
    });
    expect(useApprovalStore.getState().byId.size).toBe(0);
    expect(useApprovalStore.getState().pendingIds).toEqual([]);
  });
});

describe('approvalStore：REST 恢复（AC-006-5 挂起跨刷新）', () => {
  it('upsertFromRest 重建 pendingIds；保留事件侧已知的 toolCallId/runId', () => {
    useApprovalStore.getState().upsertPending(PENDING_BASE);
    useApprovalStore.getState().upsertFromRest([approvalItem()]);

    const entry = useApprovalStore.getState().byId.get('ap-1');
    expect(entry).toMatchObject({ status: 'pending', toolCallId: 'call_j1k2l3', runId: 'run-1' });
    expect(useApprovalStore.getState().pendingIds).toEqual(['ap-1']);
  });

  it('REST 行含已决策条目 → 只进 byId 不进 pendingIds', () => {
    useApprovalStore.getState().upsertFromRest([
      approvalItem({ id: 'ap-done', status: 'auto_approved', decision: 'approve' }),
      approvalItem({ id: 'ap-pending' }),
    ]);

    expect(useApprovalStore.getState().pendingIds).toEqual(['ap-pending']);
    expect(useApprovalStore.getState().byId.get('ap-done')).toMatchObject({
      status: 'auto_approved',
      decision: 'approve',
    });
  });
});

describe('approvalStore：REST 合并防终态回退（P2 回归）', () => {
  it('场景 A：本地已 approved，REST 陈旧快照仍报 pending → 保持 approved 且不进 pendingIds', () => {
    useApprovalStore.getState().upsertPending(PENDING_BASE);
    useApprovalStore.getState().applyResolved({
      approvalId: 'ap-1',
      decision: 'approve',
      decidedBy: 'local',
    });

    // 恢复流程的并发窗口：GET /approvals 响应快照早于本地决策落库
    useApprovalStore.getState().upsertFromRest([approvalItem()]);

    const store = useApprovalStore.getState();
    expect(store.byId.get('ap-1')).toMatchObject({
      status: 'approved',
      decision: 'approve',
      decidedBy: 'local',
    });
    expect(store.pendingIds).toEqual([]);
  });

  it('场景 B：本地 pending，REST 报终态（auto_approved）→ 以终态为准并移出 pendingIds', () => {
    useApprovalStore.getState().upsertPending(PENDING_BASE);

    useApprovalStore.getState().upsertFromRest([
      approvalItem({ id: 'ap-1', status: 'auto_approved', decision: 'approve' }),
    ]);

    const store = useApprovalStore.getState();
    expect(store.byId.get('ap-1')).toMatchObject({ status: 'auto_approved', decision: 'approve' });
    expect(store.pendingIds).toEqual([]);
  });

  it('场景 C：本地两条 pending，REST（按 session 过滤）只回一条 → 并集语义保留未回条目', () => {
    useApprovalStore.getState().upsertPending({ ...PENDING_BASE, id: 'ap-a' });
    useApprovalStore.getState().upsertPending({ ...PENDING_BASE, id: 'ap-b' });

    useApprovalStore.getState().upsertFromRest([approvalItem({ id: 'ap-a' })]);

    // 语义选择：pendingIds = REST pending ∪ 本地仍 pending 项（并集 + 终态剪枝）。
    // GET /approvals 按 session_id 过滤，「不在响应里」≠「已处理」（跨会话挂起项、
    // SSE 先于 REST 到达的窗口都不允许丢）；锁定为不整体替换。
    expect(useApprovalStore.getState().pendingIds).toEqual(['ap-a', 'ap-b']);
  });

  it('场景 D（回归）：空 store 拉回一批 → pendingIds = REST pending 子集（稳定顺序）', () => {
    useApprovalStore.getState().upsertFromRest([
      approvalItem({ id: 'ap-2' }),
      approvalItem({ id: 'ap-1' }),
      approvalItem({ id: 'ap-3', status: 'auto_approved', decision: 'approve' }),
    ]);

    expect(useApprovalStore.getState().pendingIds).toEqual(['ap-2', 'ap-1']);
  });
});

describe('approvalStore：deciding 提交态', () => {
  it('setDeciding 增删互不影响他人', () => {
    useApprovalStore.getState().setDeciding('ap-1', true);
    useApprovalStore.getState().setDeciding('ap-2', true);
    expect(useApprovalStore.getState().deciding.has('ap-1')).toBe(true);

    useApprovalStore.getState().setDeciding('ap-1', false);
    expect(useApprovalStore.getState().deciding.has('ap-1')).toBe(false);
    expect(useApprovalStore.getState().deciding.has('ap-2')).toBe(true);
  });
});
