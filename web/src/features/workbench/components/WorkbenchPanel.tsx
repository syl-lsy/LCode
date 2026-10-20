import { useCallback, useRef } from 'react';

import { useWorkbenchStore, PANEL_WIDTH_MAX, PANEL_WIDTH_MIN } from '../store';

import { PanelTabs } from './PanelTabs';
import { PlaceholderPane } from './PlaceholderPane';
import { TerminalView } from '../terminal/TerminalView';

/**
 * 右栏多用面板壳（A4 §8.1 WorkbenchPanel 行 / §9.1；R10）。
 *
 * - 贴右全高；收起 = 宽度 0 + 滑出画布右缘（collapsed/width 由 workbenchStore
 *   持久化，A4 §9.1 localStorage `lcode.panel.*`）；
 * - 左缘拖拽分隔条：220–480px，拖拽实时写 workbenchStore.width（change 即持久化）；
 * - 标签内容按 kind 分发：terminal → TerminalView；placeholder → PlaceholderPane；
 *   review/subagent 为 M3/M4 落位，先以占位呈现（不伪造功能）。
 */

const DRAG_HANDLE_PX = 5;

/** tabKind → M2 内容分发；review/subagent 的完整面板 M3/M4 接入。 */
function TabContent({ kind, title }: { kind: string; title: string }) {
  if (kind === 'terminal') return <TerminalView />;
  if (kind === 'review') {
    return (
      <PlaceholderPane
        title="审查"
        milestone="M3"
        description="diff_ready 就绪后，逐文件改动与裁决将在这里呈现。"
      />
    );
  }
  if (kind === 'subagent') {
    return <PlaceholderPane title="子智能体进度" milestone="M4" description="task() 子智能体的流式进度将在这里呈现。" />;
  }
  return <PlaceholderPane title={title} milestone="P1" />;
}

export function WorkbenchPanel() {
  const width = useWorkbenchStore((s) => s.width);
  const collapsed = useWorkbenchStore((s) => s.collapsed);
  const tabs = useWorkbenchStore((s) => s.tabs);
  const activeTabId = useWorkbenchStore((s) => s.activeTabId);
  const setWidth = useWorkbenchStore((s) => s.setWidth);

  const draggingRef = useRef(false);

  const onDragStart = useCallback(
    (event: React.MouseEvent): void => {
      event.preventDefault();
      draggingRef.current = true;
      const onMouseMove = (moveEvent: MouseEvent): void => {
        if (!draggingRef.current) return;
        // 贴右全高：宽度 = 视口宽 − 指针 x（§9.1），clamp 到 220–480
        setWidth(window.innerWidth - moveEvent.clientX);
      };
      const stop = (): void => {
        draggingRef.current = false;
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', stop);
      };
      window.addEventListener('mousemove', onMouseMove);
      window.addEventListener('mouseup', stop);
    },
    [setWidth],
  );

  if (collapsed) {
    return (
      <section
        data-testid="workbench-panel"
        data-collapsed="true"
        aria-hidden="true"
        className="w-0 overflow-hidden border-l-0"
      />
    );
  }

  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs.at(-1) ?? null;
  const widthPx = Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, width));

  return (
    <section
      data-testid="workbench-panel"
      data-collapsed="false"
      className="relative flex h-full min-w-0 flex-none overflow-hidden border-l border-border bg-panel"
      style={{ width: widthPx }}
    >
      <div
        data-testid="panel-drag-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="拖拽调整面板宽度"
        title="拖拽调整面板宽度"
        className="absolute top-0 left-0 z-30 h-full cursor-col-resize hover:bg-accent-border"
        style={{ width: DRAG_HANDLE_PX, marginLeft: -DRAG_HANDLE_PX }}
        onMouseDown={onDragStart}
      />
      <div className="relative flex h-full min-w-0 flex-1 flex-col">
        <PanelTabs />
        <div data-testid="panel-body" className="relative min-h-0 flex-1 overflow-y-auto">
          {activeTab === null ? (
            <PlaceholderPane
              title="侧边面板"
              milestone="按需"
              description="点右上「＋ 新增标签」打开终端或占位面板。"
            />
          ) : (
            <TabContent kind={activeTab.kind} title={activeTab.title} />
          )}
        </div>
      </div>
    </section>
  );
}
