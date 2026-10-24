// @vitest-environment jsdom
/**
 * Git 分支切换器（F23，toolbar-menus 菜单 6 静态占位，B5）：
 * 搜索分支 combobox（本地过滤）+ 分支组（main ✓）+ 创建置灰 + Git 图谱置灰。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { BranchSwitcher } from './BranchSwitcher';
import '../../../tests/test-utils';

describe('BranchSwitcher · Git 分支切换器（F23）', () => {
  it('main 钮弹轻量 dialog：搜索框 + main 选中 + 创建/Git 图谱置灰占位', async () => {
    const user = userEvent.setup();
    render(<BranchSwitcher />);

    await user.click(screen.getByTestId('home-branch-chip'));
    // 搜索分支 combobox
    const search = screen.getByTestId('branch-search');
    expect(search).toHaveAttribute('placeholder', '搜索分支');
    // 分支组：main 选中（勾在最前）
    const mainRow = screen.getByTestId('menu-item-branch-main');
    expect(mainRow).toHaveTextContent('main');
    expect(mainRow).toHaveAttribute('aria-checked', 'true');
    expect(mainRow.firstChild).toHaveClass('ck');
    // 创建并检出新分支 / Git 图谱：禁用占位
    expect(screen.getByTestId('menu-item-create-branch')).toBeDisabled();
    expect(screen.getByTestId('menu-item-git-graph')).toBeDisabled();
    expect(screen.getByTestId('menu-item-git-graph')).toHaveAttribute(
      'title',
      'LCode P1 占位：Git 图谱视图未排期',
    );
  });

  it('搜索框对占位分支表做本地过滤；无匹配时分支组清空', async () => {
    const user = userEvent.setup();
    render(<BranchSwitcher />);
    await user.click(screen.getByTestId('home-branch-chip'));

    await user.type(screen.getByTestId('branch-search'), 'ma');
    expect(screen.getByTestId('menu-item-branch-main')).toBeInTheDocument();

    await user.type(screen.getByTestId('branch-search'), 'zzz');
    expect(screen.queryByTestId('menu-item-branch-main')).not.toBeInTheDocument();
    // 搜索行静态结构与组头仍在（.search 行 + 分支 hdr）
    expect(screen.getByTestId('menu-item-hdr-branches')).toBeInTheDocument();
    expect(within(screen.getByTestId('menu-item-search')).getByTestId('branch-search')).toBeInTheDocument();
  });
});
