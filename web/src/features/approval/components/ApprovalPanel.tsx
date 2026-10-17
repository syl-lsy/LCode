/**
 * 审批空壳（A4 §2.1 features/approval，REQ-006）。
 * M3 接入：ApprovalCard/DecisionForm（四决策 + scope）+ approvalStore
 * （pending map + 幂等去重，AC-006-1~5）；卡片渲染在 chat 消息流内（容器例外）。
 */
export function ApprovalPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 p-4">
      <span className="font-mono text-[length:var(--text-ui-xs)] text-foreground-subtle">
        approval · 工具审批
      </span>
      <span className="font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        M3 接入（卡片挂载于 chat 流）
      </span>
    </div>
  );
}
