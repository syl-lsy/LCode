import { useState } from 'react';

import { useWorkbenchStore, type TabKind } from '../store';

/**
 * 标签条（A4 §8.1 PanelTabs 行；R10/RD#45 通用容器）：功能标签 + 「新增标签」
 * 面板内右上选择器 + 收起钮。M2 开放集合里可建的标签：终端（功能标签）、
 * 浏览器/辅助对话（P1 置灰占位）；审查标签 M3 由 diff 流程经 openTab 打开。
 */

interface ChooserEntry {
  readonly key: string;
  readonly label: string;
  readonly disabled: boolean;
  readonly kind: TabKind;
  readonly id: string;
  readonly title: string;
}

const CHOOSER_ENTRIES: readonly ChooserEntry[] = [
  { key: 'terminal', label: '终端', disabled: false, kind: 'terminal', id: 'main', title: '终端' },
  { key: 'browser', label: '浏览器', disabled: false, kind: 'placeholder', id: 'browser', title: '浏览器' },
  { key: 'assist', label: '辅助对话', disabled: false, kind: 'placeholder', id: 'assist', title: '辅助对话' },
];

export function PanelTabs() {
  const tabs = useWorkbenchStore((s) => s.tabs);
  const activeTabId = useWorkbenchStore((s) => s.activeTabId);
  const setActiveTab = useWorkbenchStore((s) => s.setActiveTab);
  const closeTab = useWorkbenchStore((s) => s.closeTab);
  const openTab = useWorkbenchStore((s) => s.openTab);
  const setCollapsed = useWorkbenchStore((s) => s.setCollapsed);
  const [chooserOpen, setChooserOpen] = useState(false);

  return (
    <div data-testid="panel-tabs" className="flex h-10 flex-none items-center gap-0.5 border-b border-border pr-1 pl-1.5">
      <div data-testid="panel-tab-strip" className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            data-testid="panel-tab"
            data-active={tab.id === activeTabId}
            className={`flex h-[26px] flex-none items-center gap-1 rounded-sm px-2 text-[length:var(--text-ui-2xs)] whitespace-nowrap ${
              tab.id === activeTabId
                ? 'border border-border bg-card text-foreground-strong'
                : 'text-foreground-subtle hover:bg-surface-hover'
            }`}
          >
            <button type="button" className="max-w-[120px] truncate" onClick={() => setActiveTab(tab.id)}>
              {tab.title}
            </button>
            <button
              type="button"
              aria-label={`关闭 ${tab.title}`}
              data-testid="panel-tab-close"
              className="flex h-4 w-4 items-center justify-center rounded-full text-foreground-subtlest hover:bg-surface-hover hover:text-foreground-strong"
              onClick={() => closeTab(tab.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        data-testid="panel-add-tab"
        aria-label="新增标签"
        aria-expanded={chooserOpen}
        className="icon-btn h-[22px] w-[22px] text-[12px]"
        onClick={() => setChooserOpen((v) => !v)}
      >
        ＋
      </button>
      <button
        type="button"
        data-testid="panel-collapse"
        aria-label="收起侧边面板"
        className="icon-btn h-[22px] w-[22px] text-[12px]"
        onClick={() => setCollapsed(true)}
      >
        ▸
      </button>
      {chooserOpen && (
        <div
          data-testid="panel-chooser"
          className="absolute top-2 right-2 z-40 w-[218px] rounded-lg border border-border-hover bg-popover p-3.5 shadow-lg"
        >
          <p className="mb-1 text-[length:var(--text-ui-base)] font-semibold text-foreground-strong">打开标签页</p>
          <p className="mb-3 text-[length:var(--text-ui-2xs)] text-foreground-subtle">选择要在侧边面板中打开的标签。</p>
          {CHOOSER_ENTRIES.map((entry) => (
            <button
              key={entry.key}
              type="button"
              data-testid={`panel-chooser-${entry.key}`}
              className="mb-1.5 flex h-9 w-full items-center gap-2 rounded-md border border-border bg-card px-2.5 text-[length:var(--text-ui-xs)] hover:border-border-hover hover:bg-surface-hover"
              onClick={() => {
                openTab({ kind: entry.kind, id: entry.id, title: entry.title });
                setChooserOpen(false);
              }}
            >
              {entry.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
