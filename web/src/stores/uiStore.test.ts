// @vitest-environment jsdom
/**
 * uiStore（A4 §9.4 双主题/双字号；A4 §9.1 持久化键前缀 `lcode.`）：
 * 主题写 html.dark 类 + localStorage；双字号写根元素 CSS 变量并 clamp
 * （界面 10-24、代码 8-20，font-controls.js 契约）；侧栏折叠持久化。
 *
 * 注意：store 是模块级单例，测试先 setTheme 显式对齐 DOM/状态再断言
 * （resetAllStores 清 DOM 类，但 store 内的 theme 字段不随 localStorage 复位）。
 */
import { describe, expect, it, beforeEach } from 'vitest';

import { CODE_FONT_MAX, CODE_FONT_MIN, UI_FONT_MAX, UI_FONT_MIN, initUiDom, useUiStore } from './uiStore';
import '../tests/test-utils';

describe('uiStore · 双主题（A4 §9.4）', () => {
  it('setTheme 切 html.dark 类并持久化 lcode.theme；默认暗色', () => {
    useUiStore.getState().setTheme('dark'); // 对齐 DOM（store 初始即 dark）
    expect(useUiStore.getState().theme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);

    useUiStore.getState().setTheme('light');
    expect(useUiStore.getState().theme).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(localStorage.getItem('lcode.theme')).toBe('light');

    useUiStore.getState().toggleTheme();
    expect(useUiStore.getState().theme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(localStorage.getItem('lcode.theme')).toBe('dark');
  });

  it('initUiDom：挂载前把主题/字号落到 DOM（防 FOUC 入口，main.tsx 调用点）', () => {
    document.documentElement.className = '';
    document.documentElement.style.cssText = '';
    initUiDom('light', 16, 13);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(document.documentElement.style.getPropertyValue('--ui-font-size')).toBe('16px');
    expect(document.documentElement.style.getPropertyValue('--code-font-size')).toBe('13px');
    initUiDom('dark', 14, 12);
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });
});

describe('uiStore · 双字号（R2：界面/代码两独立设置）', () => {
  beforeEach(() => {
    useUiStore.getState().setUiFontSize(14);
    useUiStore.getState().setCodeFontSize(12);
  });

  it('setUiFontSize：写根元素变量 + localStorage，边界 clamp 10-24', () => {
    useUiStore.getState().setUiFontSize(16);
    expect(useUiStore.getState().uiFontSize).toBe(16);
    expect(document.documentElement.style.getPropertyValue('--ui-font-size')).toBe('16px');
    expect(localStorage.getItem('lcode.font.ui')).toBe('16');

    useUiStore.getState().setUiFontSize(99);
    expect(useUiStore.getState().uiFontSize).toBe(UI_FONT_MAX);
    useUiStore.getState().setUiFontSize(0);
    expect(useUiStore.getState().uiFontSize).toBe(UI_FONT_MIN);
  });

  it('setCodeFontSize：与界面字号独立，边界 clamp 8-20', () => {
    useUiStore.getState().setUiFontSize(18);
    useUiStore.getState().setCodeFontSize(15);
    expect(useUiStore.getState().codeFontSize).toBe(15);
    expect(useUiStore.getState().uiFontSize).toBe(18); // 互不影响
    expect(document.documentElement.style.getPropertyValue('--code-font-size')).toBe('15px');

    useUiStore.getState().setCodeFontSize(1);
    expect(useUiStore.getState().codeFontSize).toBe(CODE_FONT_MIN);
    useUiStore.getState().setCodeFontSize(99);
    expect(useUiStore.getState().codeFontSize).toBe(CODE_FONT_MAX);
    expect(localStorage.getItem('lcode.font.code')).toBe(String(CODE_FONT_MAX));
  });

  it('持久化键与读取契约：setter 写 lcode.font.ui/code（init 读同键，main.tsx 装载时恢复）', () => {
    useUiStore.getState().setUiFontSize(20);
    useUiStore.getState().setCodeFontSize(9);
    expect(localStorage.getItem('lcode.font.ui')).toBe('20');
    expect(localStorage.getItem('lcode.font.code')).toBe('9');
  });
});

describe('uiStore · 侧栏折叠（A4 §9.1）', () => {
  it('toggleSidebar 持久化 lcode.sidebar（"1" = 折叠）', () => {
    const initial = useUiStore.getState().sidebarCollapsed;
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(!initial);
    expect(localStorage.getItem('lcode.sidebar')).toBe(initial ? '0' : '1');
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(initial);
  });
});
