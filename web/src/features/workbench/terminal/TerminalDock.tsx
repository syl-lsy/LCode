import { useState } from 'react';

import { useTerminalStore } from '../store';

import { TerminalView } from './TerminalView';

/**
 * 主列底部终端 dock（F2 真形；R10「任何面板在窗口底部都是错误的」唯一例外，RD#15）。
 *
 * 两态：dock 条（一行「终端」，点击展开）↔ ~200px 展开区（输出 + 收起/关闭）。
 * dock 在主列内渲染 → 宽度自然止于右栏边界（不全宽）。与右栏终端功能标签共享
 * terminalStore（同一份回显，两个容器并存互不混淆）。
 *
 * 本地开合态（折叠记忆属布局细节，不入 store）；「× 关闭」把整个 dock 收成条外
 * 不可见由父级控制——M2 简化：关闭 = 回到 dock 条。
 */
export function TerminalDock() {
  const [expanded, setExpanded] = useState(false);
  const lifecyclePhase = useTerminalStore((s) => s.lifecycle?.phase ?? null);
  const hasOutput = useTerminalStore((s) => s.execOrder.length > 0);

  if (!expanded) {
    return (
      <div data-testid="terminal-dock" className="flex-none border-t border-border bg-panel">
        <button
          type="button"
          data-testid="terminal-dock-expand"
          className="flex h-7 items-center gap-1.5 rounded-sm px-2.5 font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtle hover:bg-surface-hover hover:text-foreground-strong"
          onClick={() => setExpanded(true)}
        >
          <span aria-hidden="true">❯_</span>
          终端
          {lifecyclePhase !== null && (
            <span
              data-testid="terminal-dock-phase"
              className={`ml-1 inline-block h-[5px] w-[5px] rounded-full ${
                lifecyclePhase === 'paused' || lifecyclePhase === 'killed' || lifecyclePhase === 'crashed'
                  ? 'bg-warning'
                  : 'bg-success'
              }`}
              aria-label={`沙箱 ${lifecyclePhase}`}
            />
          )}
          {hasOutput && (
            <span aria-hidden="true" className="text-foreground-subtlest">
              ▴
            </span>
          )}
        </button>
      </div>
    );
  }

  return (
    <div data-testid="terminal-dock" className="flex-none border-t border-border bg-panel">
      <div className="flex items-center gap-1.5 border-b border-border px-2 py-0.5">
        <button
          type="button"
          data-testid="terminal-dock-collapse"
          className="flex h-6 items-center gap-1.5 rounded-sm px-2 font-mono text-[length:var(--text-ui-2xs)] text-foreground-strong hover:bg-surface-hover"
          onClick={() => setExpanded(false)}
          aria-expanded="true"
        >
          <span aria-hidden="true">❯_</span>
          终端
          <span aria-hidden="true" className="text-foreground-subtlest">
            ▾
          </span>
        </button>
        <span className="badge flex-none">只读回显</span>
        <span className="flex-1" />
        <button
          type="button"
          data-testid="terminal-dock-close"
          aria-label="收起终端 dock"
          className="icon-btn h-5 w-5 text-[11px]"
          onClick={() => setExpanded(false)}
        >
          ×
        </button>
      </div>
      <div data-testid="terminal-dock-panel" style={{ height: 200 }} className="flex flex-col overflow-hidden">
        <TerminalView />
      </div>
    </div>
  );
}
