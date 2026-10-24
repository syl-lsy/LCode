import { create } from 'zustand';

/**
 * ⌘K 面板开合状态（F5/F25 合并面板：标签页切换器 + 命令建议；B6）。
 *
 * 触发方：App 全局 Ctrl/Cmd+K、侧栏「搜索」钮（F25：官方搜索钮实测指向本面板）。
 * CommandPalette 组件消费 open 渲染浮层；状态是纯 UI 壳层开关（非会话域数据），
 * 归 src/stores/（与 uiStore/connStore 同层的壳层状态），供跨 feature 触发。
 */
export interface PaletteStore {
  open: boolean;
  openPalette: () => void;
  closePalette: () => void;
  togglePalette: () => void;
}

export const usePaletteStore = create<PaletteStore>((set) => ({
  open: false,
  openPalette: () => set({ open: true }),
  closePalette: () => set({ open: false }),
  togglePalette: () => set((s) => ({ open: !s.open })),
}));
