// @vitest-environment jsdom
/**
 * ApprovalCard（AC-006-1~5；approval-card.html 视觉）：
 * - pending 完整卡：工具标签/等待确认/risk 徽标/reason/args 原文完整 + DecisionForm；
 * - 五种终态折叠为一行，文案正确（auto_approved=「已自动通过」）；
 * - store 载荷缺失退化为占位行（fallbackStatus）；testid 契约保持 approval-row。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useApprovalStore, type ApprovalEntry, type ApprovalStatus } from '../store';
import '../../../tests/test-utils';

import { ApprovalCard } from './ApprovalCard';

const ID = 'aprv-card-1';

function entry(overrides: Partial<ApprovalEntry> = {}): Omit<
  ApprovalEntry,
  'status' | 'decision' | 'editedArgs' | 'feedback' | 'decidedBy' | 'scope'
> {
  return {
    id: ID,
    sessionId: 'sess-1',
    tool: 'execute',
    args: { command: 'rm -rf build/', timeout: 120 },
    riskLevel: 'high',
    reason: '命中危险命令规则：rm -rf',
    createdAt: '2026-10-18T00:00:00+00:00',
    toolCallId: 'call_1',
    runId: 'run-1',
    ...overrides,
  };
}

describe('ApprovalCard · pending 完整卡', () => {
  it('载荷齐备：工具标签 + 等待确认 + high 风险徽标 + reason + args 原文完整可见 + 决策表单', () => {
    useApprovalStore.getState().upsertPending(entry());
    render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);

    const row = screen.getByTestId('approval-row');
    expect(row).toHaveAttribute('data-status', 'pending');
    expect(screen.getByTestId('approval-risk')).toHaveAttribute('data-risk', 'high');
    expect(screen.getByTestId('approval-risk')).toHaveTextContent('高风险');
    expect(screen.getByTestId('approval-reason')).toHaveTextContent('命中危险命令规则：rm -rf');
    // args 原文完整（不截断、键值俱在）
    const args = screen.getByTestId('approval-args');
    expect(args).toHaveTextContent('"command": "rm -rf build/"');
    expect(args).toHaveTextContent('"timeout": 120');
    expect(screen.getByTestId('decision-form')).toBeInTheDocument();
  });

  it('medium 风险徽标（write_file 变体）', () => {
    useApprovalStore.getState().upsertPending(entry({ tool: 'write_file', riskLevel: 'medium' }));
    render(<ApprovalCard approvalId={ID} tool="write_file" fallbackStatus="pending" />);
    expect(screen.getByTestId('approval-risk')).toHaveAttribute('data-risk', 'medium');
    expect(screen.getByTestId('approval-risk')).toHaveTextContent('中风险');
  });
});

describe('ApprovalCard · 终态折叠', () => {
  const terminalCases: readonly {
    readonly status: Exclude<ApprovalStatus, 'pending'>;
    readonly label: string;
  }[] = [
    { status: 'approved', label: '已允许' },
    { status: 'rejected', label: '已拒绝' },
    { status: 'edited', label: '已编辑后允许' },
    { status: 'responded', label: '已回复' },
    { status: 'auto_approved', label: '已自动通过' },
  ];

  it.each(terminalCases)('$status → 一行「$label」，无决策表单', ({ status, label }) => {
    if (status === 'auto_approved') {
      // auto_approved 仅来自后端分类器落行，前端经 REST upsert 进入
      useApprovalStore.getState().upsertFromRest([
        {
          id: ID,
          session_id: 'sess-1',
          tool: 'execute',
          args: { command: 'x' },
          risk_level: 'medium',
          status: 'auto_approved',
          decision: 'approve',
          created_at: '2026-10-18T00:00:00+00:00',
        },
      ]);
    } else {
      useApprovalStore.getState().upsertPending(entry());
      useApprovalStore.getState().applyResolved({
        approvalId: ID,
        decision:
          status === 'approved' ? 'approve' : status === 'rejected' ? 'reject' : status === 'edited' ? 'edit' : 'respond',
        decidedBy: 'local',
      });
    }

    render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);
    const row = screen.getByTestId('approval-row');
    expect(row).toHaveAttribute('data-status', status);
    expect(row).toHaveTextContent(label);
    expect(screen.queryByTestId('decision-form')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-args')).not.toBeInTheDocument();
  });

  it('auto_approved 不落到「已回复」（AC-013-3 文案口径）', () => {
    useApprovalStore.getState().upsertFromRest([
      {
        id: ID,
        session_id: 'sess-1',
        tool: 'write_file',
        args: { path: 'a.py' },
        risk_level: 'medium',
        status: 'auto_approved',
        created_at: '2026-10-18T00:00:00+00:00',
      },
    ]);
    const { container } = render(<ApprovalCard approvalId={ID} tool="write_file" fallbackStatus="pending" />);
    expect(screen.getByTestId('approval-row')).toHaveTextContent('已自动通过');
    expect(container.textContent).not.toContain('已回复');
  });
});

describe('ApprovalCard · 载荷缺失退化', () => {
  it('store 无条目：按 fallbackStatus 渲染 pending 占位行，不出决策表单', () => {
    render(<ApprovalCard approvalId="aprv-missing" tool="execute" fallbackStatus="pending" />);
    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-status', 'pending');
    expect(screen.queryByTestId('decision-form')).not.toBeInTheDocument();
  });

  it('store 无条目但 fallbackStatus 为终态：直接折叠一行', () => {
    render(<ApprovalCard approvalId="aprv-missing" tool="execute" fallbackStatus="edited" />);
    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-status', 'edited');
    expect(screen.getByTestId('approval-row')).toHaveTextContent('已编辑后允许');
  });

  it('决策成功（applyResolved）后由完整卡原地折叠为终态行（不重复渲染两张卡）', () => {
    useApprovalStore.getState().upsertPending(entry());
    const view = render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);
    expect(screen.getByTestId('decision-form')).toBeInTheDocument();

    useApprovalStore.getState().applyResolved({ approvalId: ID, decision: 'approve', decidedBy: 'local' });
    view.rerender(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);
    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-status', 'approved');
    expect(view.container.querySelectorAll('[data-testid="approval-row"]')).toHaveLength(1);
  });
});
