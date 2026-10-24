// @vitest-environment jsdom
/**
 * 帮助菜单（F24，toolbar-menus 菜单 7，B5）：两分组条目逐字照原型，
 * 全部置灰占位（「在 Finder 中打开」RD#9 本地化口径）。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { HelpMenu } from './HelpMenu';
import '../tests/test-utils';

describe('HelpMenu · 帮助菜单（F24）', () => {
  it('两分组九条目逐字呈现，条目全部置灰占位', async () => {
    const user = userEvent.setup();
    render(<HelpMenu triggerTestId="rail-help" />);

    const trigger = screen.getByTestId('rail-help');
    expect(trigger).toHaveAttribute('aria-label', '帮助');
    await user.click(trigger);

    const menu = screen.getByTestId('menu-popover');
    const productItems = ['产品文档', '用户社群', '问题上报', '提交漏洞', '给产品提需求'];
    const appItems = ['导出日志', '在 Finder 中打开', '检查更新', '关于 LCode'];
    for (const label of [...productItems, ...appItems]) {
      expect(within(menu).getByText(label)).toBeInTheDocument();
    }
    // 分隔线落位：产品向 5 条 ｜ 应用向 4 条
    expect(within(menu).getAllByTestId('menu-item-sep-app')).toHaveLength(1);
    // 全部置灰（Web 无落地页/桌面语义）
    for (const [key, label] of [
      ['docs', '产品文档'],
      ['finder', '在 Finder 中打开'],
      ['about', '关于 LCode'],
    ] as const) {
      const item = within(menu).getByTestId(`menu-item-${key}`);
      expect(item).toBeDisabled();
      expect(item).toHaveTextContent(label);
    }
    expect(within(menu).getByTestId('menu-item-finder')).toHaveAttribute(
      'title',
      'Web 形态无本地文件系统语义，P2 桌面壳启用',
    );
  });
});
