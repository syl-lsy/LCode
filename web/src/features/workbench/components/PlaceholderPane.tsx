import type { ReactNode } from 'react';

import { EmptyState } from '@/components/EmptyState';

/**
 * P1 面板置灰空态（A4 §8.1 PlaceholderPane 行；浏览器/辅助对话等 P1 占位）。
 * 通用容器里任何尚未落地的标签类型都以此呈现，不伪造功能。
 */
export interface PlaceholderPaneProps {
  title: string;
  milestone: string;
  icon?: ReactNode;
  description?: string;
}

export function PlaceholderPane({ title, milestone, icon, description }: PlaceholderPaneProps) {
  return (
    <div data-testid="placeholder-pane" className="flex h-full items-center justify-center">
      <EmptyState
        icon={icon ?? <span aria-hidden="true">◍</span>}
        title={`${title} · ${milestone} 接入`}
        description={
          description ??
          '该面板按里程碑排期，当前为占位展示；事件与契约就绪后此处替换为完整面板。'
        }
      />
    </div>
  );
}
