import type { ReactNode } from 'react';

/**
 * 空态容器基件（empty-states.html 七态的容器基件，A4 §8.1「空态族」）。
 * 图标 + 标题 + 描述 + 可选动作插槽；纯展示，样式按原型 .frame-body 规格。
 */
export interface EmptyStateProps {
  /** 状态图标（16px 内联 svg 或字符） */
  icon?: ReactNode;
  title: string;
  description?: string;
  /** 动作区（按钮等，调用方组装） */
  action?: ReactNode;
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div
      data-testid="empty-state"
      className="flex flex-col items-center justify-center gap-1.5 px-5 py-6 text-center"
    >
      {icon !== undefined && (
        <span
          aria-hidden="true"
          className="mb-1 flex h-[34px] w-[34px] items-center justify-center rounded-md bg-surface text-foreground-subtlest"
        >
          {icon}
        </span>
      )}
      <p className="text-[length:var(--text-ui-sm)] font-medium text-foreground-strong">{title}</p>
      {description !== undefined && (
        <p className="max-w-[340px] text-[length:var(--text-ui-xs)] leading-relaxed text-foreground-subtlest">
          {description}
        </p>
      )}
      {action !== undefined && <div className="mt-2">{action}</div>}
    </div>
  );
}
