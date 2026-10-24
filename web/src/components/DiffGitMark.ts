import type { DiffReviewDetail } from '@contracts/rest';

/** 契约文件摘要条目（DiffReviewDetail.files）的 change 字面量。 */
export type DiffFileChange = DiffReviewDetail['files'][number]['change'];

/**
 * change → Git 状态方块字母 + 色值（changed-files.html .git-mark；F15/R3§F 行结构）。
 *
 * 纯展示常量（components/ 零运行时依赖约束：仅 @contracts 类型引用）；颜色取
 * --color-git-* 语义 token（亮暗双主题由 tokens.css :root/.dark 切换）；
 * 契约 change 枚举无 untracked（--color-git-untracked 仅图例口径）。
 */
export const GIT_MARK: Record<
  DiffFileChange,
  { readonly letter: string; readonly color: string }
> = {
  added: { letter: 'A', color: 'var(--color-git-added)' },
  modified: { letter: 'M', color: 'var(--color-git-modified)' },
  deleted: { letter: 'D', color: 'var(--color-git-deleted)' },
  renamed: { letter: 'R', color: 'var(--color-git-renamed)' },
};
