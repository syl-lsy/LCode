// @vitest-environment jsdom
/**
 * ApprovalCard（AC-006-1~5；approval-card.html 视觉，B3）：
 * - pending 完整卡：ap-head 工具 tag/等待确认/风险徽标 + reason + 变体主体
 *   （Bash→approval-cmd；文件写入→approval-file；其余工具→args JSON 兜底）+ DecisionForm；
 * - 五种终态折叠为一行，文案正确（auto_approved=「已自动通过」）；
 * - 拒绝且带反馈（F14 拒绝闭环）：折叠为「终端」完成态工具行 + 改道气泡；
 * - store 载荷缺失退化为占位行（fallbackStatus）；testid 契约保持 approval-row。
 */
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

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

beforeEach(() => {
  useApprovalStore.getState().reset();
});

describe('ApprovalCard · pending 完整卡（Bash 变体）', () => {
  it('载荷齐备：need-p 提示 + Bash tag/等待确认 + high 风险徽标 + reason + 命令原文 + 决策表单', () => {
    useApprovalStore.getState().upsertPending(entry());
    render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);

    const row = screen.getByTestId('approval-row');
    expect(row).toHaveAttribute('data-status', 'pending');
    expect(row).toHaveAttribute('data-variant', 'bash');
    // .need-p 前置提示（approval-card.html）
    expect(screen.getByText('需要权限')).toBeInTheDocument();
    // .ap-head：Bash tag + 等待确认
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('等待确认')).toBeInTheDocument();
    expect(screen.getByTestId('approval-risk')).toHaveAttribute('data-risk', 'high');
    expect(screen.getByTestId('approval-risk')).toHaveTextContent('高风险');
    expect(screen.getByTestId('approval-reason')).toHaveTextContent('命中危险命令规则：rm -rf');
    // .ap-cmd：命令原文完整（$ + command）
    expect(screen.getByTestId('approval-cmd')).toHaveTextContent('rm -rf build/');
    expect(screen.getByTestId('decision-form')).toBeInTheDocument();
    // Bash 变体不再渲染 JSON 兜底块与文件行
    expect(screen.queryByTestId('approval-args')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-file')).not.toBeInTheDocument();
  });
});

describe('ApprovalCard · pending 完整卡（文件写入变体）', () => {
  it('write_file：写入 tag + 文件行（文件名 + 写入行数徽标 + 目录）', () => {
    useApprovalStore.getState().upsertPending(
      entry({
        tool: 'write_file',
        riskLevel: 'medium',
        args: { path: 'obs-demo/hello.py', content: 'print("hi")\nprint(2)' },
        reason: null,
      }),
    );
    render(<ApprovalCard approvalId={ID} tool="write_file" fallbackStatus="pending" />);

    expect(screen.getByTestId('approval-row')).toHaveAttribute('data-variant', 'file');
    expect(screen.getByText('写入')).toBeInTheDocument();
    expect(screen.getByTestId('approval-risk')).toHaveAttribute('data-risk', 'medium');
    expect(screen.getByTestId('approval-risk')).toHaveTextContent('中风险');
    const fileRow = screen.getByTestId('approval-file');
    expect(fileRow).toHaveTextContent('hello.py');
    expect(screen.getByTestId('approval-file-add')).toHaveTextContent('+2');
    expect(fileRow).toHaveTextContent('obs-demo/');
    expect(screen.queryByTestId('approval-cmd')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-args')).not.toBeInTheDocument();
  });

  it('特征提取不到（非命令非文件工具）：args JSON 原文兜底可见', () => {
    useApprovalStore.getState().upsertPending(
      entry({ tool: 'grep', args: { pattern: 'TODO', path: 'src/' } }),
    );
    render(<ApprovalCard approvalId={ID} tool="grep" fallbackStatus="pending" />);

    const args = screen.getByTestId('approval-args');
    expect(args).toHaveTextContent('"pattern": "TODO"');
    expect(args).toHaveTextContent('"path": "src/"');
    expect(screen.queryByTestId('approval-cmd')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-file')).not.toBeInTheDocument();
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

describe('ApprovalCard · F14 拒绝闭环（R3§E：拒绝 + 反馈 → 折叠工具行 + 改道气泡）', () => {
  it('rejected 且带反馈：折叠为「终端」完成态工具行 + reroute 气泡（通用改道文案）', () => {
    useApprovalStore.getState().upsertPending(entry({ args: { command: 'mkdir -p /root/obs-demo' } }));
    useApprovalStore.getState().applyResolved({
      approvalId: ID,
      decision: 'reject',
      feedback: '无需建目录检查那步，直接创建文件',
      decidedBy: 'local',
    });
    render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);

    const row = screen.getByTestId('approval-row');
    expect(row).toHaveAttribute('data-status', 'rejected');
    expect(row).toHaveAttribute('data-feedback', 'true');
    // 折叠态工具行：终端标签 + 命令原文
    const folded = screen.getByTestId('approval-folded-tool');
    expect(folded).toHaveTextContent('终端');
    expect(folded).toHaveTextContent('mkdir -p /root/obs-demo');
    // 改道气泡：一行流式文本位
    expect(screen.getByTestId('approval-reroute')).toHaveTextContent(
      '收到反馈：无需建目录检查那步，直接创建文件',
    );
    expect(screen.queryByTestId('decision-form')).not.toBeInTheDocument();
  });

  it('rejected 无反馈：维持普通终态行（不误入折叠闭环形态）', () => {
    useApprovalStore.getState().upsertPending(entry());
    useApprovalStore.getState().applyResolved({ approvalId: ID, decision: 'reject', decidedBy: 'local' });
    render(<ApprovalCard approvalId={ID} tool="execute" fallbackStatus="pending" />);

    expect(screen.getByTestId('approval-row')).toHaveTextContent('已拒绝');
    expect(screen.queryByTestId('approval-folded-tool')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-reroute')).not.toBeInTheDocument();
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
