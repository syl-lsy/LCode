import { Icon } from '@/components/Icon';

import { TerminalView } from './TerminalView';

/**
 * 右栏「终端」功能标签内容（F2 态 B；来源 design/prototype/terminal-panel.html，
 * 样式类 terminal/terminal-panel.css）。
 *
 * - bash 子标签行（R4§13 结构保留 + shell 类型徽标 zsh/bash，Round 3）：root/bash
 *   激活 + workspace/zsh 两个子标签为静态占位——P0 仅单条 sandbox_output 流，
 *   多 bash 会话并行归 P1，子标签不可切换（不伪造行为）；
 * - 「+ 新建」占位；「关闭终端面板」= 关闭当前终端标签（store closeTab）；
 * - 输出沿用 TerminalView 只读回显；底部输入行 = P1 交互输入形态占位（readOnly）。
 */
export function TerminalPane({
  tabId,
  onCloseTab,
}: {
  tabId: string;
  onCloseTab: (tabId: string) => void;
}) {
  return (
    <div data-testid="terminal-pane" className="flex h-full min-h-0 flex-col">
      <div className="bash-row">
        <span className="tt">终端</span>
        <div className="t-tabs">
          <span className="t-tab on" data-testid="terminal-pane-tab-root" title="root 终端（当前流）">
            root
            <span className="sh">bash</span>
            <span className="x" title="关闭 root（P1 多会话占位）" aria-hidden="true">
              <Icon name="close" size={9} />
            </span>
          </span>
          <span className="t-tab" data-testid="terminal-pane-tab-workspace" title="workspace 终端（P1 多会话占位）">
            workspace
            <span className="sh">zsh</span>
            <span className="x" title="关闭 workspace（P1 多会话占位）" aria-hidden="true">
              <Icon name="close" size={9} />
            </span>
          </span>
        </div>
        <button
          type="button"
          className="icon-btn"
          aria-disabled="true"
          title="新建终端（P1 多会话占位）"
          style={{ width: 22, height: 22 }}
        >
          <Icon name="plus" size={11} />
        </button>
        <button
          type="button"
          data-testid="terminal-pane-close"
          className="icon-btn"
          aria-label="关闭终端面板"
          title="关闭终端面板"
          style={{ width: 22, height: 22 }}
          onClick={() => onCloseTab(tabId)}
        >
          <Icon name="close" size={11} />
        </button>
      </div>
      {/* 事件: sandbox_output —— 只读回显 */}
      <TerminalView />
      {/* P0：只读回显；交互输入归 P1（输入行为 P1 形态占位，readOnly 不可输入） */}
      <div className="t-input" data-testid="terminal-pane-input">
        <span className="g">root@sandbox</span>
        <input aria-label="Terminal input" readOnly title="P0 只读回显，交互输入归 P1" />
      </div>
    </div>
  );
}
