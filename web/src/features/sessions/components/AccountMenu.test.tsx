// @vitest-environment jsdom
/**
 * 账号菜单（F16，toolbar-menus 菜单 8，B5）：头像+用户名+ID 复制真实可用；
 * 界面主题真实可切（uiStore 驱动 html.dark）；界面语言/模式置灰占位；
 * 无「界面缩放」子菜单（RD#1）。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useUiStore } from '@/stores/uiStore';

import { AccountMenu } from './AccountMenu';
import '../../../tests/test-utils';

afterEach(() => {
  useUiStore.getState().setTheme('dark'); // 主题切换跨用例复位
  vi.restoreAllMocks();
});

describe('AccountMenu · 账号菜单（F16）', () => {
  it('头像 + 用户名 + ID 行；点击复制真实 ID 并就地反馈「已复制 ✓」', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<AccountMenu />);

    await user.click(screen.getByTestId('account-button'));
    const idEl = screen.getByTestId('account-id');
    expect(idEl).toHaveTextContent('ID: 7f3a-9c2e-11d4');

    await user.click(screen.getByTestId('menu-item-acct'));
    expect(writeText).toHaveBeenCalledWith('7f3a-9c2e-11d4');
    expect(screen.getByTestId('account-id')).toHaveTextContent('已复制 ✓');
  });

  it('界面主题真实可切：选浅色 → html.dark 移除 + store 更新；跟随系统按 matchMedia 兜底', async () => {
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(screen.getByTestId('account-button'));

    // 先点深色把主题落到 DOM（测试环境的 html 初始无 dark 类，store 默认 dark 未应用）
    await user.click(screen.getByTestId('account-theme-dark'));
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    await user.click(screen.getByTestId('account-theme-light'));
    expect(useUiStore.getState().theme).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    // 深色项选中态跟随主题（勾移到浅色行）
    expect(screen.getByTestId('account-theme-light')).toHaveAttribute('aria-checked', 'true');

    // 跟随系统：jsdom 无 matchMedia → 兜底 light（保持，不抛错）
    await user.click(screen.getByTestId('account-theme-system'));
    expect(['light', 'dark']).toContain(useUiStore.getState().theme);
  });

  it('语言/模式子菜单置灰占位；升级/邀请好友/断开连接 P2 置灰；无「界面缩放」（RD#1）', async () => {
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(screen.getByTestId('account-button'));

    const menu = screen.getByTestId('menu-popover');
    expect(within(menu).getByTestId('menu-item-lang')).toHaveClass('mi', 'sub-parent');
    expect(within(menu).getByTestId('account-mode-coding')).toBeDisabled();
    expect(within(menu).getByTestId('account-mode-office')).toBeDisabled();
    for (const key of ['usage', 'upgrade', 'invite', 'disconnect']) {
      expect(within(menu).getByTestId(`menu-item-${key}`)).toBeDisabled();
    }
    expect(within(menu).getByTestId('menu-item-invite')).toHaveTextContent('奖励');
    expect(within(menu).queryByText('界面缩放')).not.toBeInTheDocument();
  });
});
