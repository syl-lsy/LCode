// @vitest-environment jsdom
/**
 * Menu 浮层基件（toolbar-menus 八菜单共用；单层菜单 F8/F7 的基件）：
 * 开关（aria-expanded）、选择回调后关闭、置灰项不可选、Escape/外部点击关闭。
 * 附带 EmptyState/Spinner 基件冒烟 + vitest-axe a11y 断言（A4 §10.1）。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { expectNoAxeViolations } from '../tests/test-utils';

import { EmptyState } from './EmptyState';
import { Menu } from './Menu';
import { Spinner } from './Spinner';

function makeItems() {
  return [
    { key: 'a', label: '选项 A', onSelect: vi.fn() },
    { key: 'b', label: '选项 B', disabled: true, onSelect: vi.fn() },
    { key: 'c', label: '选项 C', selected: true, onSelect: vi.fn() },
  ];
}

describe('Menu · 开关与键盘', () => {
  it('点击触发钮开关浮层（aria-haspopup/aria-expanded 同步）', async () => {
    const user = userEvent.setup();
    render(<Menu trigger="打开" triggerClassName="tool-btn" items={makeItems()} />);
    const trigger = screen.getByRole('button', { name: '打开' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('menu-popover')).toBeInTheDocument();
    // menuitem（动作项）+ menuitemradio（带选中态项）都算菜单项
    const menu = screen.getByTestId('menu-popover');
    expect(within(menu).getAllByRole('menuitem')).toHaveLength(2);
    expect(within(menu).getAllByRole('menuitemradio')).toHaveLength(1);
  });

  it('点选项：触发回调并关闭浮层（单层菜单语义）', async () => {
    const user = userEvent.setup();
    const items = makeItems();
    render(<Menu trigger="打开" items={items} />);
    await user.click(screen.getByRole('button', { name: '打开' }));
    await user.click(screen.getByTestId('menu-item-a'));
    expect(items[0]?.onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('menu-popover')).not.toBeInTheDocument();
  });

  it('置灰项不可选：disabled 且点击不触发回调、不关闭', async () => {
    const user = userEvent.setup();
    const items = makeItems();
    render(<Menu trigger="打开" items={items} />);
    await user.click(screen.getByRole('button', { name: '打开' }));
    const disabled = screen.getByTestId('menu-item-b');
    expect(disabled).toBeDisabled();
    await user.click(disabled);
    expect(items[1]?.onSelect).not.toHaveBeenCalled();
    expect(screen.getByTestId('menu-popover')).toBeInTheDocument();
  });

  it('selected 项带 aria-checked；hint 渲染在右侧', async () => {
    const user = userEvent.setup();
    render(
      <Menu
        trigger="打开"
        items={[{ key: 'x', label: '当前', selected: true, hint: <span>⌘1</span>, onSelect: vi.fn() }]}
      />,
    );
    await user.click(screen.getByRole('button', { name: '打开' }));
    expect(screen.getByTestId('menu-item-x')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('menu-item-x')).toHaveTextContent('⌘1');
  });

  it('Escape 与外部点击关闭', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Menu trigger="打开" items={makeItems()} />
        <button type="button" data-testid="outside">
          外
        </button>
      </div>,
    );
    await user.click(screen.getByRole('button', { name: '打开' }));
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('menu-popover')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开' }));
    await user.click(screen.getByTestId('outside'));
    expect(screen.queryByTestId('menu-popover')).not.toBeInTheDocument();
  });
});

describe('EmptyState / Spinner · 基件', () => {
  it('EmptyState：图标/标题/描述/动作插槽', () => {
    render(
      <EmptyState
        icon={<span aria-hidden="true">◍</span>}
        title="还没有任务"
        description="新建任务后，这里会展示任务与状态。"
        action={<button type="button">新建</button>}
      />,
    );
    const el = screen.getByTestId('empty-state');
    expect(el).toHaveTextContent('还没有任务');
    expect(el).toHaveTextContent('新建任务后');
    expect(screen.getByRole('button', { name: '新建' })).toBeInTheDocument();
  });

  it('Spinner：role=status + 可定制 label', () => {
    render(<Spinner label="正在连接" />);
    expect(screen.getByRole('status', { name: '正在连接' })).toBeInTheDocument();
  });

  it('a11y：Menu 打开态无 axe 违例（交互组件基线）', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <Menu trigger="模型" triggerClassName="tool-btn" triggerLabel="模型选择器" items={makeItems()} />,
    );
    await user.click(screen.getByRole('button', { name: '模型选择器' }));
    await expectNoAxeViolations(container);
  });
});
