import { useEffect } from 'react';

import { useTerminalStore, useWorkbenchStore } from '../store';

import { TerminalView } from './TerminalView';

import { Icon } from '@/components/Icon';

/**
 * 主列底部终端 dock（F2 态 A 真形；R10「任何面板在窗口底部都是错误的」唯一例外，
 * 修订 RD#15。来源：design/prototype/terminal-panel.html，样式类
 * terminal/terminal-panel.css）。
 *
 * 三态（workbenchStore.terminalDock，UI 状态扩展不持久化）：
 *   hidden（全收，不渲染）↔ bar（dock 条：一行「终端」，点击展开）
 *   ↔ panel（~200px 展开区：终端｜zsh 徽标｜workspace ×｜+ 新建｜× 关闭 + 输出 + 输入行）。
 * dock 在主列内渲染 → 宽度自然止于右栏边界（不全宽）。
 *
 * - Ctrl/Cmd+J 全局键位（本组件挂载即生效）：hidden ↔ bar；panel 态下再按 = 全收
 *   （对齐原型 dock.hidden 判定口径）；
 * - 「× 关闭」收起整个 dock；展开区「终端」子标签收起回 dock 条；
 * - 输出沿用 TerminalView 只读回显（与右栏终端功能标签共享 terminalStore，两套
 *   容器并存互不混淆）；底部输入行 = P1 交互输入的形态占位（sandbox_output P0
 *   无输入通道，readOnly 不可输入）；
 * - 「workspace ×」「+ 新建」为占位（P1 多会话，aria-disabled + 轻提示）。
 */
export function TerminalDock() {
  const dockState = useWorkbenchStore((s) => s.terminalDock);
  const setDockState = useWorkbenchStore((s) => s.setTerminalDock);
  const toggleDock = useWorkbenchStore((s) => s.toggleTerminalDock);
  const lifecyclePhase = useTerminalStore((s) => s.lifecycle?.phase ?? null);
  const hasOutput = useTerminalStore((s) => s.execOrder.length > 0);

  // Ctrl/Cmd+J 切换终端 dock（B4 键位补齐；dock 卸载即解绑）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && (event.key === 'j' || event.key === 'J')) {
        event.preventDefault();
        toggleDock();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [toggleDock]);

  if (dockState === 'hidden') return null;

  if (dockState === 'bar') {
    return (
      <div data-testid="terminal-dock" className="dock">
        <div className="dock-bar">
          <button
            type="button"
            data-testid="terminal-dock-expand"
            className="dock-tab"
            title="展开终端区（~200px）"
            aria-expanded="false"
            onClick={() => setDockState('panel')}
          >
            <Icon name="terminal" size={11} />
            终端
            <Icon name="chevron-up" size={10} className="chev" />
            {lifecyclePhase !== null && (
              <span
                data-testid="terminal-dock-phase"
                className={`inline-block h-[5px] w-[5px] flex-none rounded-full ${
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
      </div>
    );
  }

  return (
    <div data-testid="terminal-dock" className="dock">
      <div data-testid="terminal-dock-panel" className="dock-panel" style={{ height: 200 }}>
        <div className="dock-head">
          <button
            type="button"
            data-testid="terminal-dock-collapse"
            className="dock-tab on"
            title="收起回 dock 条"
            aria-expanded="true"
            onClick={() => setDockState('bar')}
          >
            <Icon name="terminal" size={11} />
            终端
            <Icon name="chevron-down" size={10} className="chev" />
          </button>
          <span className="sh" title="shell 类型：zsh">
            zsh
          </span>
          {/* workspace 子标签 ×：P1 多会话占位（原型无行为，静态） */}
          <span className="t-tab on" title="workspace 终端">
            workspace
            <span className="x" title="关闭 workspace（P1 多会话占位）" aria-hidden="true">
              <Icon name="close" size={9} />
            </span>
          </span>
          <button
            type="button"
            className="icon-btn"
            aria-disabled="true"
            title="新建终端（P1 多会话占位）"
            style={{ width: 22, height: 22 }}
          >
            <Icon name="plus" size={11} />
          </button>
          <span className="sp" aria-hidden="true" />
          <button
            type="button"
            data-testid="terminal-dock-close"
            className="icon-btn"
            aria-label="关闭终端 dock"
            title="关闭终端 dock"
            style={{ width: 22, height: 22 }}
            onClick={() => setDockState('hidden')}
          >
            <Icon name="close" size={11} />
          </button>
        </div>
        {/* 事件: sandbox_output —— TerminalView 只读回显（dock 与右栏终端共享同一份） */}
        <div className="flex min-h-0 flex-1 flex-col">
          <TerminalView />
        </div>
        {/* P0：只读回显；交互输入归 P1（输入行为 P1 形态占位，readOnly 不可输入） */}
        <div className="t-input" data-testid="terminal-dock-input">
          <span className="g">root@sandbox</span> <span className="b">obs-demo</span> %
          <input aria-label="Terminal input" readOnly title="P0 只读回显，交互输入归 P1" />
        </div>
      </div>
    </div>
  );
}
