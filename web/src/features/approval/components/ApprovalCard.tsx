import type { ApprovalStatus } from '../store';
import { useApprovalStore } from '../store';

import { DecisionForm } from './DecisionForm';

/**
 * 审批卡片（A4 §8.1 ApprovalCard 行；approval-card.html 两变体）。
 *
 * - props 只收结构化字段（approvalId / tool / fallbackStatus），不跨 feature
 *   import chat 类型；完整载荷（args/riskLevel/reason/status）按 id 从
 *   approvalStore 读取——大载荷不进 chat item（§8.2 容器/卡片分工）；
 * - pending：完整卡（工具标签 + 等待确认 + risk 徽标 + reason + args 原文完整
 *   可见 + DecisionForm）；store 载荷未加载（如刷新后 SSE 未回放）时退化为
 *   占位行，不出决策表单（缺载荷不可决策）；
 * - 终态（approved/rejected/edited/responded/auto_approved）：折叠为一行结果，
 *   auto_approved 文案「已自动通过」（不落到「已回复」）；
 * - 根节点保留 data-testid="approval-row" + data-status（MessageList/replay
 *   既有定位契约）。
 */

const TERMINAL_LABELS: Readonly<Record<Exclude<ApprovalStatus, 'pending'>, string>> = {
  approved: '已允许',
  rejected: '已拒绝',
  edited: '已编辑后允许',
  responded: '已回复',
  auto_approved: '已自动通过',
};

export interface ApprovalCardProps {
  readonly approvalId: string;
  /** 工具名（chat item 冗余的轻量字段；载荷缺失时仍可显示） */
  readonly tool: string;
  /** chat item 持有的状态快照；store 载荷存在时以 store 为准 */
  readonly fallbackStatus: ApprovalStatus;
}

export function ApprovalCard({ approvalId, tool, fallbackStatus }: ApprovalCardProps) {
  const entry = useApprovalStore((s) => s.byId.get(approvalId));
  const status: ApprovalStatus = entry?.status ?? fallbackStatus;

  if (status !== 'pending') {
    return (
      <div
        data-testid="approval-row"
        data-status={status}
        className="mb-2.5 flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle"
      >
        <span aria-hidden="true" className="flex-none text-success">
          ✓
        </span>
        <span className="flex-none">权限审批 ·</span>
        <span className="flex-none font-mono text-foreground">{tool}</span>
        <span className="ml-auto flex-none">{TERMINAL_LABELS[status]}</span>
      </div>
    );
  }

  if (entry === undefined) {
    // 载荷未加载（store 缺条目）：占位行不出决策表单（AC-006-5 恢复由 App 拉
    // GET /approvals?status=pending 补齐，补上后原地升级为完整卡）
    return (
      <div
        data-testid="approval-row"
        data-status="pending"
        className="mb-2.5 flex items-center gap-2 rounded-md border border-accent-border bg-accent px-3 py-2 text-[length:var(--text-ui-xs)] text-accent-foreground"
      >
        <span className="flex-none">权限审批 ·</span>
        <span className="flex-none font-mono">{tool}</span>
        <span className="flex-none">等待确认</span>
        <span className="ml-auto flex-none text-foreground-subtlest">载入中…</span>
      </div>
    );
  }

  return (
    <div
      data-testid="approval-row"
      data-status="pending"
      className="mb-2.5 overflow-hidden rounded-lg border border-accent-border bg-card"
    >
      <div className="flex items-center gap-2 border-b border-border bg-accent px-3 py-2">
        <span className="rounded-sm border border-accent-border px-1.5 py-0.5 font-mono text-[length:var(--text-ui-2xs)] text-accent-foreground">
          {entry.tool}
        </span>
        <span className="text-[length:var(--text-ui-xs)] text-accent-foreground">等待确认</span>
        <span className="ml-auto flex-none">
          {entry.riskLevel === 'high' ? (
            <span data-testid="approval-risk" data-risk="high" className="badge border-destructive/40 text-destructive">
              高风险
            </span>
          ) : (
            <span data-testid="approval-risk" data-risk="medium" className="badge border-warning/40 text-warning">
              中风险
            </span>
          )}
        </span>
      </div>

      {entry.reason !== null && (
        <p
          data-testid="approval-reason"
          className="border-b border-border px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle"
        >
          {entry.reason}
        </p>
      )}

      <pre
        data-testid="approval-args"
        className="overflow-x-auto bg-terminal-bg px-3 py-2.5 font-mono text-[length:var(--code-font-size)] leading-relaxed break-all whitespace-pre-wrap text-terminal-fg"
      >
        {JSON.stringify(entry.args, null, 2)}
      </pre>

      <DecisionForm approvalId={approvalId} args={entry.args} />
    </div>
  );
}
