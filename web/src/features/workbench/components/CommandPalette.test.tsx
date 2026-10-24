// @vitest-environment jsdom
/**
 * ⌘K 面板（F5/F25 合并面板，B6）：
 * - 「打开的标签页」组（任务 tab + 面板 tab + 当前态高亮 + × 关闭）；
 * - 选中真实切换：面板 tab → workbenchStore 激活 + 展开面板；任务 tab × → 回首页；
 * - 过滤 / 范围（全部/操作/任务）/ 键盘（↑↓/Enter/Esc/Tab）；
 * - 命令项：已有能力接真实行为（新任务回调/切换侧边栏），无匹配 → 空态（empty-states #5）。
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import '../../../tests/test-utils';

import { usePaletteStore } from '@/stores/paletteStore';
import { useUiStore } from '@/stores/uiStore';

import { TASK_TAB_ID, useWorkbenchStore } from '../store';

import { CommandPalette } from './CommandPalette';

/** 挂在探针路由下：任务 tab 导航/关闭可断言路由变化。 */
function renderPalette(props: Partial<Parameters<typeof CommandPalette>[0]> = {}): ReturnType<typeof createMemoryRouter> {
  const router = createMemoryRouter(
    [
      {
        path: '/s/:sessionId',
        element: (
          <CommandPalette
            sessionId="sess-a"
            sessionTitle="修复登录超时"
            taskTabTime="5 分钟前"
            {...props}
          />
        ),
      },
      { path: '/', element: <div data-testid="home-probe" /> },
    ],
    { initialEntries: ['/s/sess-a'] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

/** 种子：任务 tab 激活 + 审查/终端两个面板标签。 */
function seedTabs(): void {
  useWorkbenchStore.getState().openReviewTab('rev-1', '审查 · 第 1 轮');
  useWorkbenchStore.getState().openTab({ kind: 'terminal', id: 'main', title: '终端' });
  useWorkbenchStore.getState().setActiveTab(TASK_TAB_ID);
}

afterEach(() => {
  usePaletteStore.getState().closePalette();
});

describe('CommandPalette · 打开的标签页（F5）', () => {
  it('打开渲染：任务条目（标题 + 相对时间）+ 面板条目 + 当前激活 tab 高亮 + 底部提示', () => {
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    const dlg = screen.getByTestId('command-palette');
    expect(dlg).toHaveAttribute('role', 'dialog');
    const taskOpt = screen.getByTestId('command-palette-opt-task:current');
    expect(taskOpt).toHaveTextContent('修复登录超时');
    expect(taskOpt).toHaveTextContent('5 分钟前');
    expect(taskOpt).toHaveAttribute('aria-selected', 'true'); // 当前态高亮（光标初始落激活 tab）
    const reviewOpt = screen.getByTestId('command-palette-opt-review:rev-1');
    expect(reviewOpt).toHaveTextContent('审查 · 第 1 轮');
    expect(reviewOpt).toHaveTextContent('面板');
    expect(reviewOpt).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByTestId('command-palette-opt-terminal:main')).toHaveTextContent('终端');
    // 「打开的标签页」组头 + 底部键位提示（原型 foot 逐字）
    expect(screen.getByText('打开的标签页')).toBeInTheDocument();
    expect(screen.getByText('Ctrl K 唤起')).toBeInTheDocument();
  });

  it('面板 tab 选中 = 真实切换：激活该标签 + 展开面板 + 关闭浮层', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    await user.click(screen.getByTestId('command-palette-opt-review:rev-1'));
    expect(useWorkbenchStore.getState().activeTabId).toBe('review:rev-1');
    expect(useWorkbenchStore.getState().collapsed).toBe(false);
    expect(usePaletteStore.getState().open).toBe(false);
  });

  it('面板 tab × = 关闭标签（浮层保持，可继续切换）', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    await user.click(screen.getByTestId('command-palette-close-terminal:main'));
    expect(useWorkbenchStore.getState().tabs.some((tab) => tab.id === 'terminal:main')).toBe(false);
    expect(usePaletteStore.getState().open).toBe(true);
  });

  it('任务 tab × = 关闭任务标签（离开会话视图回首页；浮层开合不受影响）', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    const router = renderPalette();

    await user.click(screen.getByTestId('command-palette-close-task:current'));
    await waitFor(() => expect(screen.getByTestId('home-probe')).toBeInTheDocument());
    expect(usePaletteStore.getState().open).toBe(true); // × 只关标签，浮层开合不受影响
    expect(router.state.location.pathname).toBe('/');
  });

  it('任务条目选中 = 会话路由导航（已在当前会话 → 停留 + 关闭浮层）', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    const router = renderPalette();

    await user.click(screen.getByTestId('command-palette-opt-task:current'));
    await waitFor(() => expect(router.state.location.pathname).toBe('/s/sess-a'));
    expect(usePaletteStore.getState().open).toBe(false);
  });
});

describe('CommandPalette · 过滤 / 范围 / 键盘', () => {
  it('输入过滤命中子串；无匹配 → 空态（empty-states #5 文案照抄）', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    await user.type(screen.getByTestId('command-palette-input'), '审查');
    expect(screen.getByTestId('command-palette-opt-review:rev-1')).toBeInTheDocument();
    expect(screen.queryByTestId('command-palette-opt-terminal:main')).not.toBeInTheDocument();

    await user.clear(screen.getByTestId('command-palette-input'));
    await user.type(screen.getByTestId('command-palette-input'), '不存在的关键词');
    expect(screen.getByTestId('command-palette-empty')).toHaveTextContent('没有匹配的命令');
    expect(screen.getByTestId('command-palette-empty')).toHaveTextContent('换个关键词，或用 Tab 切换范围');
  });

  it('范围标签：任务 = 只显示标签页组；操作 = 只显示命令组；Tab 键循环范围', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    await user.click(screen.getByTestId('command-palette-scope-tasks'));
    expect(screen.getByTestId('command-palette-opt-task:current')).toBeInTheDocument();
    expect(screen.queryByText('建议')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('command-palette-scope-actions'));
    expect(screen.queryByTestId('command-palette-opt-task:current')).not.toBeInTheDocument();
    expect(screen.getByTestId('command-palette-opt-new-task')).toBeInTheDocument();

    // Tab 键循环：操作 → 任务 → 全部
    await user.type(screen.getByTestId('command-palette-input'), '{Tab}');
    expect(screen.getByTestId('command-palette-scope-tasks')).toHaveAttribute('aria-selected', 'true');
    await user.type(screen.getByTestId('command-palette-input'), '{Tab}');
    expect(screen.getByTestId('command-palette-scope-all')).toHaveAttribute('aria-selected', 'true');
  });

  it('键盘导航：↑↓ 移动光标，Enter 执行（新任务回调 + 关闭浮层）', async () => {
    const user = userEvent.setup();
    const onNewTask = vi.fn();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette({ onNewTask });

    const input = screen.getByTestId('command-palette-input');
    // 光标初始在任务条目（index 0）→ ↓ 到审查 tab → ↓ 到终端 tab → ↓ 到「新任务」
    await user.type(input, '{ArrowDown}{ArrowDown}{ArrowDown}{Enter}');
    expect(onNewTask).toHaveBeenCalledTimes(1);
    expect(usePaletteStore.getState().open).toBe(false);
  });

  it('Esc 关闭；已有能力命令接真实行为（切换侧边栏 → uiStore）', async () => {
    const user = userEvent.setup();
    seedTabs();
    usePaletteStore.getState().openPalette();
    renderPalette();

    await user.type(screen.getByTestId('command-palette-input'), '{Escape}');
    expect(usePaletteStore.getState().open).toBe(false);

    act(() => {
      usePaletteStore.getState().openPalette();
    });
    const before = useUiStore.getState().sidebarCollapsed;
    await user.click(screen.getByTestId('command-palette-opt-toggle-sidebar'));
    expect(useUiStore.getState().sidebarCollapsed).toBe(!before);
    expect(usePaletteStore.getState().open).toBe(false);
  });

  it('置灰命令占位：审查标签不可手开（title 轻提示 + aria-disabled）', () => {
    usePaletteStore.getState().openPalette();
    renderPalette();

    const addReview = screen.getByTestId('command-palette-opt-add-review');
    expect(addReview).toHaveAttribute('aria-disabled', 'true');
    expect(addReview).toHaveAttribute('title', '审查标签由 diff_ready 流程自动打开');
  });
});
