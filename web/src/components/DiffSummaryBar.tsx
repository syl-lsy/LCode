import type { DiffReadyEvent } from '@contracts/events';

/**
 * DiffSummaryBar（M3 Phase 4 diff 审查地基）：diff 摘要条，可复用于面板顶部。
 *
 * 字段对齐契约 DiffReadyEvent / DiffReviewDetail 的摘要面：round、baseline
 * （kind/ref，commit 不上 UI）、additions/deletions 总量、文件数；binary 文件
 * 数 > 0 时单独提示（对齐 DiffFileDetail.binary：二进制文件无文本 diff）。
 * 样式复用 tokens.css 的 .badge / .badge-add / .badge-del 共享组件类。
 */

type BaselineRef = DiffReadyEvent['baseline'];
type FileSummary = DiffReadyEvent['files'][number];

export interface DiffSummaryBarProps {
  readonly round: number;
  readonly baseline: BaselineRef;
  /** 文件摘要（DiffFileSummary[]）；仅消费 binary 计数与长度 */
  readonly files: readonly FileSummary[];
  readonly additionsTotal: number;
  readonly deletionsTotal: number;
}

export function DiffSummaryBar({ round, baseline, files, additionsTotal, deletionsTotal }: DiffSummaryBarProps) {
  const binaryCount = files.filter((f) => f.binary === true).length;
  return (
    <div
      data-testid="diff-summary-bar"
      className="flex flex-wrap items-center gap-2 text-[length:var(--text-ui-xs)]"
    >
      <span data-testid="diff-summary-add" className="badge badge-add">
        +{additionsTotal}
      </span>
      <span data-testid="diff-summary-del" className="badge badge-del">
        -{deletionsTotal}
      </span>
      <span data-testid="diff-summary-files" className="badge">
        {files.length} 个文件
      </span>
      <span data-testid="diff-summary-round" className="badge">
        第 {round} 轮
      </span>
      <span data-testid="diff-summary-baseline" className="badge" title={`commit ${baseline.commit}`}>
        基线 {baseline.kind}/{baseline.ref}
      </span>
      {binaryCount > 0 && (
        <span data-testid="diff-summary-binary" className="badge">
          含 {binaryCount} 个二进制文件
        </span>
      )}
    </div>
  );
}
