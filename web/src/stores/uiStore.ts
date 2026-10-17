import { create } from 'zustand';

export type Theme = 'dark' | 'light';

interface UiState {
  /** 主题（默认暗色，A4 §9.4）；初始值从 index.html 防 FOUC 脚本设置好的 html.dark 类读取。 */
  theme: Theme;
  /** 左栏折叠态（A4 §9.1，可折叠为窄 rail）。 */
  sidebarCollapsed: boolean;
  setTheme: (theme: Theme) => void;
  toggleSidebar: () => void;
}

function initialTheme(): Theme {
  if (typeof document === 'undefined') return 'dark';
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

/**
 * 基础设施 store：主题/侧栏（A4 §3.2 uiStore 行的骨架子集）。
 * M2 任务 2 补齐：双字号（uiFontSize/codeFontSize）、面板宽度、localStorage
 * 持久化（键前缀 `lcode.`）与 html.dark 类写入副作用（A4 §9.1/§9.4）。
 */
export const useUiStore = create<UiState>((set) => ({
  theme: initialTheme(),
  sidebarCollapsed: false,
  setTheme: (theme) => set({ theme }),
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
}));
