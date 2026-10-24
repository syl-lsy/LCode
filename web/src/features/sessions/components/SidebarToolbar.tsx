import { Icon } from '@/components/Icon';
import { useUiStore } from '@/stores/uiStore';

/**
 * 侧栏工具条：会话计数 + 刷新 + 主题/双字号快捷控件（R2 双字号与主题的 M2 入口；
 * 设置页 P1 落地前先在侧栏提供最小可用控件）。
 *
 * 图标口径（B0）：字符/emoji 图标（A±/ａ±/☀☾/⟳）统一换 Icon 描边 SVG——
 * 界面字号用 font-size（A 字形）、代码字号用 terminal（`>_` 字形，区分两域），
 * 增/减以图标大小示意（小=减、大=增）；主题按当前主题显示对侧图标（暗→日、亮→月）。
 */
export interface SidebarToolbarProps {
  count: number;
  onRefresh: () => void;
  refreshing?: boolean;
}

const UI_FONT_STEP = 1;
const CODE_FONT_STEP = 1;

export function SidebarToolbar({ count, onRefresh, refreshing = false }: SidebarToolbarProps) {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const uiFontSize = useUiStore((s) => s.uiFontSize);
  const codeFontSize = useUiStore((s) => s.codeFontSize);
  const setUiFontSize = useUiStore((s) => s.setUiFontSize);
  const setCodeFontSize = useUiStore((s) => s.setCodeFontSize);

  return (
    <div data-testid="sidebar-toolbar" className="flex items-center gap-1 px-2.5 pt-1.5 pb-1">
      <span className="flex-none text-[length:var(--text-ui-xs)] font-semibold text-foreground-strong">
        会话
      </span>
      <span data-testid="session-count" className="badge flex-none">
        {count}
      </span>
      <span className="flex-1" />
      <button
        type="button"
        data-testid="font-ui-dec"
        aria-label="减小界面字号"
        title="减小界面字号"
        className="icon-btn h-6 w-6"
        onClick={() => setUiFontSize(uiFontSize - UI_FONT_STEP)}
      >
        <Icon name="font-size" size={10} />
      </button>
      <button
        type="button"
        data-testid="font-ui-inc"
        aria-label="增大界面字号"
        title="增大界面字号"
        className="icon-btn h-6 w-6"
        onClick={() => setUiFontSize(uiFontSize + UI_FONT_STEP)}
      >
        <Icon name="font-size" size={15} />
      </button>
      <button
        type="button"
        data-testid="font-code-dec"
        aria-label="减小代码字号"
        title="减小代码字号"
        className="icon-btn h-6 w-6"
        onClick={() => setCodeFontSize(codeFontSize - CODE_FONT_STEP)}
      >
        <Icon name="terminal" size={10} />
      </button>
      <button
        type="button"
        data-testid="font-code-inc"
        aria-label="增大代码字号"
        title="增大代码字号"
        className="icon-btn h-6 w-6"
        onClick={() => setCodeFontSize(codeFontSize + CODE_FONT_STEP)}
      >
        <Icon name="terminal" size={15} />
      </button>
      <button
        type="button"
        data-testid="theme-toggle"
        aria-label={theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
        className="icon-btn h-6 w-6"
        onClick={toggleTheme}
      >
        {theme === 'dark' ? <Icon name="sun" size={13} /> : <Icon name="moon" size={13} />}
      </button>
      <button
        type="button"
        data-testid="session-refresh"
        aria-label="刷新会话列表"
        title="刷新会话列表"
        disabled={refreshing}
        className="icon-btn h-6 w-6"
        onClick={onRefresh}
      >
        <Icon name="refresh" size={13} />
      </button>
    </div>
  );
}
