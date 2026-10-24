import type { ReactNode } from 'react';

/**
 * 空态容器基件（empty-states.html 七态的容器基件，A4 §8.1「空态族」）。
 * 图标 + 标题 + 描述 + 可选动作插槽；纯展示。
 *
 * B6 扩展（对照 design/prototype/empty-states.html 一比一）：
 * - 传入 `head` 时启用「frame 变体」：整体包原型 .frame 容器（card 底 + 描边 +
 *   radius-lg），头部渲染 .frame-head 工具行（badge/标题文字/工具钮，由调用方
 *   组装），本体迁移为原型类 .frame-body/.empty-ic/.et/.ed（样式类在
 *   styles/components.css B6 段）；
 * - 不传 head 保持既有 tailwind 形态（M2 以来消息流/会话列表等消费方零变化）。
 */
export interface EmptyStateProps {
  /** 状态图标（16px 内联 svg 或字符） */
  icon?: ReactNode;
  title: string;
  description?: string;
  /** 动作区（按钮等，调用方组装） */
  action?: ReactNode;
  /**
   * frame 变体头部（原型 .frame-head 内容行：badge + .t 文字 + 工具钮）；
   * 传入即启用 .frame 容器形态（审查面板空态 / 侧栏归档模式 / 两视图空态）。
   */
  head?: ReactNode;
  /** 根节点 data-testid 覆盖（默认 empty-state；同屏多空态时区分定位） */
  testId?: string;
}

export function EmptyState({ icon, title, description, action, head, testId }: EmptyStateProps) {
  if (head !== undefined) {
    return (
      <div data-testid={testId ?? 'empty-state'} className="frame">
        <div className="frame-head">{head}</div>
        <div className="frame-body">
          {icon !== undefined && (
            <div aria-hidden="true" className="empty-ic">
              {icon}
            </div>
          )}
          <div className="et">{title}</div>
          {description !== undefined && <div className="ed">{description}</div>}
          {action !== undefined && <div className="mt-2">{action}</div>}
        </div>
      </div>
    );
  }
  return (
    <div
      data-testid={testId ?? 'empty-state'}
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
