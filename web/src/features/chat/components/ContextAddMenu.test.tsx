// @vitest-environment jsdom
/**
 * 添加上下文菜单（@）（toolbar-menus 菜单 4 静态演示，B5）：
 * 三段结构（添加/插件/文件）+ RD#8 LCode 语境占位条目 + menu-foot 提示，全部置灰。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ContextAddMenu } from './ContextAddMenu';
import '../../../tests/test-utils';

describe('ContextAddMenu · @ 上下文菜单（F4/RD#8）', () => {
  it('三段结构齐备，插件示例为 LCode 语境占位，底部提示 = 原型逐字', async () => {
    const user = userEvent.setup();
    render(<ContextAddMenu />);

    await user.click(screen.getByTestId('composer-context'));
    const menu = screen.getByTestId('menu-popover');
    // 添加段
    expect(within(menu).getByTestId('menu-item-hdr-add')).toHaveTextContent('添加');
    for (const key of ['attachment', 'goal', 'workflow']) {
      expect(within(menu).getByTestId(`menu-item-${key}`)).toBeDisabled();
    }
    // 插件段（RD#8 LCode 语境占位）
    expect(within(menu).getByText('$fetch-url')).toBeInTheDocument();
    expect(within(menu).getByText('$sandbox-files')).toBeInTheDocument();
    // 文件段：搜索中行（静态）
    expect(within(menu).getByTestId('menu-item-searching-files')).toHaveClass('searching');
    // 底部 menu-foot：三快捷键提示 + 搜索提示
    const foot = within(menu).getByTestId('menu-item-foot');
    expect(foot).toHaveTextContent('添加上下文');
    expect(foot).toHaveTextContent('选择能力');
    expect(foot).toHaveTextContent('选择技能');
    expect(within(menu).getByText('输入内容以搜索插件、文件和对话')).toBeInTheDocument();
  });
});
