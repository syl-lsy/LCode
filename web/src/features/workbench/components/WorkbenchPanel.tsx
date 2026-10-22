import { useCallback, useRef } from 'react';

import { useWorkbenchStore, PANEL_WIDTH_MAX, PANEL_WIDTH_MIN } from '../store';
import type { WorkbenchTab } from '../store';

import { DiffReviewPanel } from './DiffReviewPanel';
import { PanelTabs } from './PanelTabs';
import { PlaceholderPane } from './PlaceholderPane';
import { TerminalView } from '../terminal/TerminalView';

/**
 * 右栏多用面板壳（A4 §8.1 WorkbenchPanel 行 / §9.1；R10）。
 *
 * - 贴右全高；收起 = 宽度 0 + 滑出画布右缘（collapsed/width 由 workbenchStore
 *   持久化，A4 §9.1 localStorage `lcode.panel.*`）；
 * - 左缘拖拽分隔条：220–480px，拖拽实时写 workbenchStore.width（change 即持久化）；
 * - 标签内容按 kind 分发：terminal → TerminalView；review → DiffReviewPanel（M3，
 *   reviewId 从标签 id 解出）；placeholder → PlaceholderPane；subagent 为 M4 落位。
 */

const DRAG_HANDLE_PX = 5;

/** review 标签 id 前缀（tabIdOf('review', id) 的产物形态， 解析用）。 */
const REVIEW_TAB_PREFIX = 'review:';

/** tabKind → 内容分发。 */
function TabContent({ tab, currentSessionId }: { tab: WorkbenchTab; currentSessionId: string | null }) {
  if (tab.kind === 'terminal') return <TerminalView />;
  if (tab.kind === 'review') {
    // 标签 id = `review:<reviewId>`（openReviewTab/tabIdOf 写入），面板据此定位审查
    return <DiffReviewPanel reviewId={tab.id.slice(REVIEW_TAB_PREFIX.length)} currentSessionId={currentSessionId} />;
  }
  if (tab.kind === 'subagent') {
    return <PlaceholderPane title="子智能体进度" milestone="M4" description="task() 子智能体的流式进度将在这里呈现。" />;
  }
  return <PlaceholderPane title={tab.title} milestone="P1" />;
}

export interface WorkbenchPanelProps {
  /**
   * 当前会话 id（App 层自 sessionStore 下传；feature 隔离规则禁止 workbench 直接
   * import sessions store）。review 面板用它做会话防串台校验。缺省 null 时跳过。
   */
  readonly currentSessionId?: string | null;
}

export function WorkbenchPanel({ currentSessionId = null }: WorkbenchPanelProps) {
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
            <TabContent tab={activeTab} currentSessionId={currentSessionId} />
          )}
        </div>
      </div>
    </section>
  );
}
