import { create } from 'zustand';

/**
 * 基础设施 store：主题 / 双字号 / 侧栏折叠（A4 §3.2 uiStore 行 / §9.1 / §9.4）。
 *
 * 持久化（§9.1：localStorage，键前缀 `lcode.`）：
 * - 主题 `lcode.theme`；写入副作用 = 切 html.dark 类（原型机制，A4 §9.4）；
 * - 双字号 `lcode.font.ui` / `lcode.font.code`（font-controls.js 的 React 化契约：
 *   界面 10-24px、代码 8-20px 两独立值，写根元素 CSS 变量 --ui-font-size/--code-font-size）；
 * - 侧栏折叠 `lcode.sidebar`。
 *
 * 副作用集中在 applyUiToDom：无 document 环境（SSR/Node）静默跳过，store 逻辑可测。
 * 初始值在模块加载时读一次 localStorage；防 FOUC 的 DOM 预设由 main.tsx 在 React
 * 挂载前调用 initUiDom() 完成（同一套读取/应用逻辑，单一事实源）。
 */

export type Theme = 'dark' | 'light';

/** 双字号边界（font-controls.js 契约：界面 10-24、代码 8-20）。 */
export const UI_FONT_MIN = 10;
export const UI_FONT_MAX = 24;
export const CODE_FONT_MIN = 8;
export const CODE_FONT_MAX = 20;
export const UI_FONT_DEFAULT = 14;
export const CODE_FONT_DEFAULT = 12;

const THEME_KEY = 'lcode.theme';
const FONT_UI_KEY = 'lcode.font.ui';
const FONT_CODE_KEY = 'lcode.font.code';
const SIDEBAR_KEY = 'lcode.sidebar';

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // 隐私模式等：静默降级为默认值
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore：持久化失败不阻断交互
  }
}

function parseFontSize(raw: string | null, fallback: number, min: number, max: number): number {
  if (raw === null) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return clamp(parsed, min, max);
}

function readTheme(): Theme {
  // html.dark 类是运行时真相（防 FOUC 脚本先于 store 设置）；
  // localStorage 仅作回写目标，两者不一致时以 DOM 为准。
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
    return 'dark';
  }
  return readStorage(THEME_KEY) === 'light' ? 'light' : 'dark';
}

/** 把主题/字号落到 DOM（document 缺失时跳过）。 */
export function applyUiToDom(theme: Theme, uiFontSize: number, codeFontSize: number): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.toggle('dark', theme === 'dark');
  root.style.setProperty('--ui-font-size', `${uiFontSize}px`);
  root.style.setProperty('--code-font-size', `${codeFontSize}px`);
}

/** main.tsx 在 createRoot 之前调用：防 FOUC（React 挂载前 DOM 已是持久化的主题/字号）。 */
export function initUiDom(theme: Theme, uiFontSize: number, codeFontSize: number): void {
  applyUiToDom(theme, uiFontSize, codeFontSize);
}

interface UiState {
  theme: Theme;
  /** 界面字号 px（--ui-font-size，10-24，A4 §9.4 双字号 R2） */
  uiFontSize: number;
  /** 代码字号 px（--code-font-size，8-20，与界面字号独立） */
  codeFontSize: number;
  /** 左栏折叠态（A4 §9.1，可折叠为窄 rail） */
  sidebarCollapsed: boolean;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  setUiFontSize: (px: number) => void;
  setCodeFontSize: (px: number) => void;
  toggleSidebar: () => void;
}

export const useUiStore = create<UiState>((set, get) => ({
  theme: readTheme(),
  uiFontSize: parseFontSize(readStorage(FONT_UI_KEY), UI_FONT_DEFAULT, UI_FONT_MIN, UI_FONT_MAX),
  codeFontSize: parseFontSize(readStorage(FONT_CODE_KEY), CODE_FONT_DEFAULT, CODE_FONT_MIN, CODE_FONT_MAX),
  sidebarCollapsed: readStorage(SIDEBAR_KEY) === '1',

  setTheme: (theme) => {
    writeStorage(THEME_KEY, theme);
    applyUiToDom(theme, get().uiFontSize, get().codeFontSize);
    set({ theme });
  },

  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),

  setUiFontSize: (px) => {
    const uiFontSize = clamp(px, UI_FONT_MIN, UI_FONT_MAX);
    writeStorage(FONT_UI_KEY, String(uiFontSize));
    applyUiToDom(get().theme, uiFontSize, get().codeFontSize);
    set({ uiFontSize });
  },

  setCodeFontSize: (px) => {
    const codeFontSize = clamp(px, CODE_FONT_MIN, CODE_FONT_MAX);
    writeStorage(FONT_CODE_KEY, String(codeFontSize));
    applyUiToDom(get().theme, get().uiFontSize, codeFontSize);
    set({ codeFontSize });
  },

  toggleSidebar: () => {
    const sidebarCollapsed = !get().sidebarCollapsed;
    writeStorage(SIDEBAR_KEY, sidebarCollapsed ? '1' : '0');
    set({ sidebarCollapsed });
  },
}));
