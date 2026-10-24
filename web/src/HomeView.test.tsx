// @vitest-environment jsdom
/**
 * HomeView 空态首页（B1 原型还原 app-shell.html）：
 * - F29 时段化问候：greetingIndexForHour 五态边界 + 渲染取当前时段 + 点击循环预览；
 * - F29 L 字母水印（.wm）；
 * - C-06 成本提醒胶囊（quota-tip）：默认可见 + × 可关；
 * - 快捷入口 4 卡（.quick）：原型文案 + 点击填入 Composer 输入框；
 * - F23 项目 chip / main 分支钮：分支钮点击轻提示，项目 chip 占位禁用。
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { GREETINGS, greetingIndexForHour } from './App';
import { renderAtRoute } from './tests/test-utils';

describe('greetingIndexForHour · 五态边界（F29）', () => {
  it('清晨 05-08 / 上午 09-11 / 下午 12-17 / 傍晚 18-22 / 深夜 23-04', () => {
    expect(greetingIndexForHour(0)).toBe(4);
    expect(greetingIndexForHour(4)).toBe(4);
    expect(greetingIndexForHour(5)).toBe(0);
    expect(greetingIndexForHour(8)).toBe(0);
    expect(greetingIndexForHour(9)).toBe(1);
    expect(greetingIndexForHour(11)).toBe(1);
    expect(greetingIndexForHour(12)).toBe(2);
    expect(greetingIndexForHour(17)).toBe(2);
    expect(greetingIndexForHour(18)).toBe(3);
    expect(greetingIndexForHour(22)).toBe(3);
    expect(greetingIndexForHour(23)).toBe(4);
  });

  it('五态文案各不相同且非空', () => {
    expect(new Set(GREETINGS).size).toBe(GREETINGS.length);
    for (const text of GREETINGS) expect(text.trim().length).toBeGreaterThan(0);
  });
});

describe('HomeView · 空态首页结构（B1）', () => {
  it('渲染时段问候（当前时段五态之一）+ L 字母水印 + 快捷键提示行', () => {
    renderAtRoute('/');
    const greet = screen.getByTestId('home-greet');
    expect(GREETINGS).toContain(greet.textContent);
    // 水印：.wm 类 + 文案 L + aria-hidden
    const wm = greet.closest('.main-center')?.querySelector('.wm');
    if (wm === null) throw new Error('水印 .wm 缺失');
    expect(wm).toHaveAttribute('aria-hidden', 'true');
    expect(wm).toHaveTextContent('L');
    expect(screen.getByText(/LCode 快捷键/)).toBeInTheDocument();
  });

  it('问候点击循环预览五态（原型 demo 交互的 React 等价实现）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    const greet = screen.getByTestId('home-greet');
    const start = GREETINGS.indexOf(greet.textContent ?? '');
    if (start < 0) throw new Error('问候文案不在五态清单内');
    await user.click(greet);
    expect(greet.textContent).toBe(GREETINGS[(start + 1) % GREETINGS.length]);
  });

  it('quota-tip 胶囊默认可见，× 关闭后消失（C-06 演示件）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    expect(screen.getByTestId('quota-tip')).toHaveTextContent('本月额度剩余 64% · 11-01 重置（P2 计费前演示件）');
    await user.click(screen.getByTestId('quota-tip-close'));
    await waitFor(() => expect(screen.queryByTestId('quota-tip')).not.toBeInTheDocument());
  });

  it('快捷入口 4 卡为原型文案，点击填入 Composer 输入框', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    const cards = screen.getAllByTestId('quick-card');
    expect(cards.map((el) => el.textContent?.trim())).toEqual(['周报总结', '报错修复', 'PPT 制作', '闲时任务']);

    const [weekly, bugfix] = cards;
    if (weekly === undefined || bugfix === undefined) throw new Error('快捷卡数量不足');
    await user.click(weekly);
    expect(screen.getByTestId('composer-input')).toHaveValue('周报总结');

    await user.click(bugfix);
    expect(screen.getByTestId('composer-input')).toHaveValue('周报总结\n报错修复');
  });

  it('项目 chip / main 分支钮（F23）：分支钮弹切换器 dialog，项目 chip 与取消钮占位禁用', async () => {
    const user = userEvent.setup();
    renderAtRoute('/');
    // 输入区占位文案 = 原型口径
    expect(screen.getByTestId('composer-input')).toHaveAttribute(
      'placeholder',
      '向 LCode 提问，使用 @ 添加上下文，使用 / 选择命令或能力',
    );

    // F23 分支切换器（toolbar-menus 菜单 6）：搜索框 + 分支组 + 创建置灰 + Git 图谱置灰
    await user.click(screen.getByTestId('home-branch-chip'));
    expect(screen.getByTestId('branch-search')).toBeInTheDocument();
    const mainRow = screen.getByTestId('menu-item-branch-main');
    expect(mainRow).toHaveTextContent('main');
    expect(mainRow.firstChild).toHaveClass('ck'); // 当前分支勾在最前
    expect(screen.getByTestId('menu-item-create-branch')).toBeDisabled();
    expect(screen.getByTestId('menu-item-git-graph')).toBeDisabled();
  });
});
