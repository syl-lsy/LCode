// @vitest-environment jsdom
/**
 * 斜杠命令选择器（/）（toolbar-menus 菜单 5 静态演示，B5）：
 * 命令（5 内置）/ 技能（用户·插件 $ 前缀）/ 子智能体（4 角色带 Tools 标注行）
 * 三段 + 底部提示；纯静态置灰。
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { SlashCommandMenu } from './SlashCommandMenu';
import '../../../tests/test-utils';

describe('SlashCommandMenu · 斜杠命令三段选择器（R12/F5-F25）', () => {
  it('命令/技能/子智能体三段完整呈现，子智能体带 Tools 标注行', async () => {
    const user = userEvent.setup();
    render(<SlashCommandMenu />);

    await user.click(screen.getByTestId('composer-slash'));
    const menu = screen.getByTestId('menu-popover');
    // 命令段（R12 已核官方描述）
    for (const [key, cmd] of [
      ['cmd-goal', '/goal'],
      ['cmd-workflow', '/workflow'],
      ['cmd-compact', '/compact'],
      ['cmd-init', '/init'],
      ['cmd-plan', '/plan'],
    ] as const) {
      const row = within(menu).getByTestId(`menu-item-${key}`);
      expect(row).toHaveTextContent(cmd);
      expect(row).toBeDisabled();
    }
    expect(within(menu).getByText('Show or set the current session goal.')).toBeInTheDocument();
    // 技能段（RD#8：用户 · 与 插件 · 两类）
    expect(within(menu).getByText('$fetch-url')).toBeInTheDocument();
    expect(within(menu).getByText('用户 · 抓取网页正文并消毒进上下文…')).toBeInTheDocument();
    expect(within(menu).getByText('$commit-msg')).toBeInTheDocument();
    // 子智能体段（LCode 内置 4 角色 + Tools 标注行）
    for (const agent of ['general-purpose', 'explorer', 'coder', 'reviewer']) {
      expect(within(menu).getByText(agent)).toBeInTheDocument();
    }
    expect(within(menu).getByText('Tools: 全部工具 · 审批')).toBeInTheDocument();
    expect(within(menu).getByText('Tools: 只读 7 工具 · 免审批')).toBeInTheDocument();
    expect(within(menu).getByText('Tools: 全部工具 + 沙箱 · 审批')).toBeInTheDocument();
    expect(within(menu).getByText('Tools: 只读 7 工具 · 结构化输出')).toBeInTheDocument();
    // 底部提示 = 原型逐字
    expect(within(menu).getByText('输入内容以搜索命令、技能或子智能体')).toBeInTheDocument();
  });
});
