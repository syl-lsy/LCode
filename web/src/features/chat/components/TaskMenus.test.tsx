// @vitest-environment jsdom
/**
 * TaskMenus（F26/R4，B6）：
 * - 「更多」菜单 = 12 项 + 3 分隔线（逐字照原型），置灰占位与真实行为分野；
 * - 「在资源管理器中打开」macOS 本地化为「在 Finder 中打开」（RD#9）；
 * - 真实行为：重命名（回调）、复制会话 ID（剪贴板）、查看调用轨迹（任务名标签）；
 * - 分享勾选模式（R4）：勾选/全选/清除/Esc 残留/重新进入/取消/导出 Markdown；
 *   「分享」只读链接按钮 P2 前置灰。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import '../../../tests/test-utils';

import { useChatStore } from '../store';
import { TASK_TAB_ID, useWorkbenchStore } from '@/features/workbench/store';

import { TaskMenus, buildShareMarkdown, isMacPlatform } from './TaskMenus';

function renderMenus(props: Partial<Parameters<typeof TaskMenus>[0]> = {}): void {
  render(<TaskMenus sessionId="sess-a" sessionTitle="修复登录超时" {...props} />);
}

/** 种子：两条用户消息 + 一条助手消息（R4 勾选模式素材）。 */
function seedMessages(): void {
  useChatStore.getState().appendUserMessage({
    id: 'u-1',
    content: '在 /root/obs-demo 目录创建 hello.py，打印 Hello。',
    createdAt: '2026-10-18T00:00:00+00:00',
    runId: null,
  });
  useChatStore.getState().ensureAssistantMessage({
    id: 'a-1',
    createdAt: '2026-10-18T00:00:10+00:00',
    runId: null,
  });
  useChatStore.getState().appendUserMessage({
    id: 'u-2',
    content: '把 hello.py 改成函数式写法。',
    createdAt: '2026-10-18T00:01:00+00:00',
    runId: null,
  });
}

async function openSharePanel(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByTestId('task-share'));
  return screen.getByTestId('share-panel');
}

describe('TaskMenus · 「更多」菜单（F26：12 项 + 3 分隔线）', () => {
  it('条目逐字照原型；置灰占位与分隔线落位', async () => {
    const user = userEvent.setup();
    renderMenus();
    await user.click(screen.getByTestId('task-more'));

    const menu = screen.getByTestId('menu-popover');
    const labels = [
      '置顶任务',
      '重命名任务',
      '归档任务',
      '标记为未读',
      '在资源管理器中打开',
      '复制路径',
      '复制任务路径',
      '复制日志路径',
      '复制会话 ID',
      '前往配置',
      '查看调用轨迹',
      '反馈问题',
    ];
    for (const label of labels) {
      expect(within(menu).getByText(label)).toBeInTheDocument();
    }
    // 3 条分隔线（F26 校准：12 项 + 3 分隔线）
    expect(within(menu).getAllByRole('separator')).toHaveLength(3);
    // 置灰占位（Web 无对应能力，title 轻提示）
    for (const key of ['pin', 'archive', 'mark-unread', 'open-finder', 'copy-path', 'goto-config', 'feedback']) {
      expect(within(menu).getByTestId(`menu-item-${key}`)).toBeDisabled();
    }
    expect(within(menu).getByTestId('menu-item-open-finder')).toHaveAttribute(
      'title',
      'LCode Web 无本地文件系统语义，P2 桌面壳启用',
    );
  });

  it('「在资源管理器中打开」macOS 本地化为「在 Finder 中打开」（RD#9，仍置灰）', async () => {
    const user = userEvent.setup();
    const original = navigator.platform;
    Object.defineProperty(window.navigator, 'platform', { value: 'MacIntel', configurable: true });
    try {
      renderMenus();
      await user.click(screen.getByTestId('task-more'));
      const menu = screen.getByTestId('menu-popover');
      expect(within(menu).getByText('在 Finder 中打开')).toBeInTheDocument();
      expect(within(menu).getByTestId('menu-item-open-finder')).toBeDisabled();
    } finally {
      Object.defineProperty(window.navigator, 'platform', { value: original, configurable: true });
    }
    expect(isMacPlatform()).toBe(navigator.platform.startsWith('Mac'));
  });

  it('重命名任务：prompt 取新标题 → onRename 回调（App 接 PATCH）；取消不触发', async () => {
    const user = userEvent.setup();
    const onRename = vi.fn().mockResolvedValue(undefined);
    renderMenus({ onRename });
    await user.click(screen.getByTestId('task-more'));

    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('  新标题  ');
    await user.click(screen.getByTestId('menu-item-rename'));
    expect(promptSpy).toHaveBeenCalledWith('重命名任务', '修复登录超时');
    expect(onRename).toHaveBeenCalledWith('新标题');

    await user.click(screen.getByTestId('task-more'));
    promptSpy.mockReturnValue(null);
    await user.click(screen.getByTestId('menu-item-rename'));
    expect(onRename).toHaveBeenCalledTimes(1); // 取消不触发
    promptSpy.mockRestore();
  });

  it('复制会话 ID：写入剪贴板（真实能力）', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderMenus();
    await user.click(screen.getByTestId('task-more'));
    await user.click(screen.getByTestId('menu-item-copy-session-id'));
    expect(writeText).toHaveBeenCalledWith('sess-a');
  });

  it('查看调用轨迹：激活任务名标签（轨迹查看器 P1 预留位）+ 展开面板', async () => {
    const user = userEvent.setup();
    renderMenus();
    await user.click(screen.getByTestId('task-more'));
    await user.click(screen.getByTestId('menu-item-trajectory'));
    expect(useWorkbenchStore.getState().activeTabId).toBe(TASK_TAB_ID);
    expect(useWorkbenchStore.getState().collapsed).toBe(false);
  });
});

describe('TaskMenus · 分享勾选模式（R4）', () => {
  it('进入勾选模式：用户消息可勾选（picked 高亮 + 计数），助手消息不参与勾选', async () => {
    const user = userEvent.setup();
    seedMessages();
    renderMenus();
    await openSharePanel(user);

    const rows = screen.getAllByTestId('share-msg');
    expect(rows).toHaveLength(2); // 仅两条用户消息
    const firstRow = rows[0];
    if (firstRow === undefined) throw new Error('无用户消息行');
    expect(screen.getByText('助手回复（工具卡片与 markdown）— 助手消息不参与勾选。')).toBeInTheDocument();
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 0 条消息');

    await user.click(firstRow);
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 1 条消息');
    expect(screen.getAllByTestId('share-msg')[0]).toHaveAttribute('data-checked', 'true');
    // 「分享」只读链接按钮 P2 前置灰；导出可用
    expect(screen.getByTestId('share-link')).toBeDisabled();
    expect(screen.getByTestId('share-export')).toBeEnabled();
  });

  it('全选 / 清除；导出 Markdown 走 Blob 下载；取消清空勾选并关闭', async () => {
    const user = userEvent.setup();
    seedMessages();
    renderMenus();
    await openSharePanel(user);

    await user.click(screen.getByTestId('share-select-all'));
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 2 条消息');

    // 导出 Markdown（真实下载）
    const createObjectURL = vi.fn((blob: Blob) => `blob:mock-${blob.size}`);
    const revokeObjectURL = vi.fn();
    URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL;
    await user.click(screen.getByTestId('share-export'));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0]?.[0];
    if (blob === undefined) throw new Error('createObjectURL 未收到 Blob');
    await expect(blob.text()).resolves.toContain('在 /root/obs-demo 目录创建 hello.py');
    expect(buildShareMarkdown([{ content: '单条' }])).toContain('# LCode 会话分享导出');

    // 清除 → 计数归零；取消 → 关闭且勾选清空（无残留条）
    await user.click(screen.getByTestId('share-clear'));
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 0 条消息');
    await user.click(screen.getByTestId('share-cancel'));
    expect(screen.queryByTestId('share-panel')).not.toBeInTheDocument();
    expect(screen.queryByTestId('share-resid-wrap')).not.toBeInTheDocument();
  });

  it('Esc 关闭面板：勾选态保留（R4 残留态），残留按钮可重新进入', async () => {
    const user = userEvent.setup();
    seedMessages();
    renderMenus();
    await openSharePanel(user);
    const firstRow = screen.getAllByTestId('share-msg')[0];
    if (firstRow === undefined) throw new Error('无用户消息行');
    await user.click(firstRow);
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 1 条消息');

    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('share-panel')).not.toBeInTheDocument();
    // 残留态：勾选保留 + 重新进入按钮
    expect(screen.getByTestId('share-resid-wrap')).toBeInTheDocument();
    await user.click(screen.getByTestId('share-reopen'));
    expect(screen.getByTestId('share-count')).toHaveTextContent('已选 1 条消息');
  });

  it('遮罩点击关闭（勾选保留）；空会话给出可分享提示', async () => {
    const user = userEvent.setup();
    renderMenus();
    await openSharePanel(user);
    expect(screen.getByText('会话暂无可分享的消息。')).toBeInTheDocument();
    expect(screen.getByTestId('share-export')).toBeDisabled();

    await user.click(screen.getByTestId('share-backdrop'));
    await waitFor(() => expect(screen.queryByTestId('share-panel')).not.toBeInTheDocument());
  });
});
