// @vitest-environment jsdom
/**
 * 右栏多用面板壳（A4 §9.1 / §8.1；R10/RD#45）：
 * 收起（宽度 0）↔ 展开（220-480 拖拽）；标签条（任务名首标签 F25 + 功能/产物标签）
 * 新增/切换/关闭；终端标签渲染 TerminalPane（态 B bash 子标签行）；占位标签渲染
 * 结构化 PlaceholderPane；无标签时给「打开标签页」选择器空态而非空白。
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { PANEL_WIDTH_MAX, PANEL_WIDTH_MIN, TASK_TAB_ID, useDiffStore, useWorkbenchStore } from '../store';
import '../../../tests/test-utils';

import { makeDiffFile, makeDiffReview, setDiffFilesFixture, setDiffReviewsFixture } from '../../../tests/msw-handlers';

import { WorkbenchPanel } from './WorkbenchPanel';

beforeEach(() => {
  // 标签/激活/折叠是模块级 store：逐用例复位（localStorage 一并清，防持久化键串场）
  localStorage.clear();
  useWorkbenchStore.getState().reset();
  useDiffStore.getState().reset();
});

describe('WorkbenchPanel · 收起/展开与拖拽（R10）', () => {
  it('默认收起：宽度 0、aria-hidden、不渲染内容', () => {
    render(<WorkbenchPanel />);
    const panel = screen.getByTestId('workbench-panel');
    expect(panel).toHaveAttribute('data-collapsed', 'true');
    expect(panel).toHaveClass('w-0');
    expect(screen.queryByTestId('panel-tabs')).not.toBeInTheDocument();
  });

  it('展开：按 store 宽度渲染标签条 + 无标签时「打开标签页」选择器空态', () => {
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    const panel = screen.getByTestId('workbench-panel');
    expect(panel).toHaveAttribute('data-collapsed', 'false');
    expect(panel).toHaveStyle({ width: `${useWorkbenchStore.getState().width}px` });
    expect(screen.getByTestId('panel-tabs')).toBeInTheDocument();
    // 面板空态 = 原型「打开标签页」选择器（.chooser）
    expect(screen.getByTestId('panel-empty-chooser')).toHaveTextContent('打开标签页');
  });

  it('面板空态选择器：八类可开类型 + 「…」开放集合禁用占位（RD#45）；点「终端」开终端标签；「审查」不可手开', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    const chooser = screen.getByTestId('panel-empty-chooser');

    // RD#45 全目录八类可开类型（任务名为会话首标签不在列）
    for (const key of ['assist', 'review', 'terminal', 'browser', 'file-preview', 'code-view', 'plan', 'subagent']) {
      expect(within(chooser).getByTestId(`panel-chooser-${key}`)).toBeInTheDocument();
    }
    // 尾部「…」= 更多类型以产物为准（禁用占位）
    const more = within(chooser).getByTestId('panel-chooser-more');
    expect(more).toBeDisabled();
    expect(more).toHaveAttribute('title', expect.stringContaining('更多类型以产物为准'));

    // 点「终端」直接开终端标签
    await user.click(within(chooser).getByTestId('panel-chooser-terminal'));
    expect(screen.getByTestId('panel-tab')).toHaveTextContent('终端');
    expect(screen.getByTestId('terminal-pane')).toBeInTheDocument();
    expect(screen.queryByTestId('panel-empty-chooser')).not.toBeInTheDocument();

    // 关闭终端标签（store 自动收起面板）→ 再展开：审查项 aria-disabled 不可手开
    act(() => {
      useWorkbenchStore.getState().closeTab('terminal:main');
      useWorkbenchStore.getState().setCollapsed(false);
    });
    const reviewBtn = screen
      .getByTestId('panel-empty-chooser')
      .querySelector('button[aria-disabled="true"]');
    expect(reviewBtn).not.toBeNull();
    expect(reviewBtn).toHaveAttribute('title', '审查标签由 diff_ready 流程自动打开');
  });

  it('拖拽分隔条：mousemove 实时写宽（clamp 220-480），mouseup 结束', () => {
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    const handle = screen.getByTestId('panel-drag-handle');

    fireEvent.mouseDown(handle, { preventDefault: () => undefined });
    // 视口 1024 宽：指针 x=700 → 期望 324
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true });
    fireEvent.mouseMove(window, { clientX: 700 });
    expect(useWorkbenchStore.getState().width).toBe(324);
    // 拖到极窄 → clamp 到 480（1024-100=924 → 480 上限）
    fireEvent.mouseMove(window, { clientX: 100 });
    expect(useWorkbenchStore.getState().width).toBe(PANEL_WIDTH_MAX);
    // 拖到极宽 → clamp 到 220（1024-1000=24 → 220 下限）
    fireEvent.mouseMove(window, { clientX: 1000 });
    expect(useWorkbenchStore.getState().width).toBe(PANEL_WIDTH_MIN);

    fireEvent.mouseUp(window);
    fireEvent.mouseMove(window, { clientX: 500 }); // 结束后不再生效
    expect(useWorkbenchStore.getState().width).toBe(PANEL_WIDTH_MIN);
  });

  it('收起钮：setCollapsed(true) → 面板回到宽度 0（localStorage 持久化由 store 单测覆盖）', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    await user.click(screen.getByTestId('panel-collapse'));
    expect(useWorkbenchStore.getState().collapsed).toBe(true);
    expect(screen.getByTestId('workbench-panel')).toHaveAttribute('data-collapsed', 'true');
  });
});

describe('任务名首标签（F25/RD#45）', () => {
  it('有会话上下文：tablist 首位渲染任务名标签（无 ×）；点击激活任务概要占位块', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel sessionTitle="修复登录超时" />);

    const taskTab = screen.getByTestId('panel-task-tab');
    expect(taskTab).toBeInTheDocument();
    expect(taskTab).toHaveTextContent('修复登录超时');
    // 无关闭语义（RD#45：会话首标签不进选择器、无 ×）
    expect(within(taskTab).queryByTestId('panel-tab-close')).not.toBeInTheDocument();

    await user.click(taskTab);
    expect(taskTab).toHaveClass('on');
    expect(useWorkbenchStore.getState().activeTabId).toBe(TASK_TAB_ID);
    expect(screen.getByTestId('task-summary-pane')).toBeInTheDocument();
  });

  it('打开真实标签后任务名标签让位激活态（伪标签不入 tabs 集合）', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel sessionTitle="修复登录超时" />);

    // 空标签时激活任务名标签
    await user.click(screen.getByTestId('panel-task-tab'));
    expect(screen.getByTestId('task-summary-pane')).toBeInTheDocument();

    // 从浮层选择器开终端标签 → 激活态让位终端标签（任务名标签仍在首位）
    await user.click(screen.getByTestId('panel-search-tab'));
    await user.click(screen.getByTestId('panel-chooser-terminal'));
    expect(screen.queryByTestId('task-summary-pane')).not.toBeInTheDocument();
    expect(screen.getByTestId('panel-task-tab')).not.toHaveClass('on');
    expect(screen.getByTestId('terminal-pane')).toBeInTheDocument();
  });

  it('无会话上下文（首页）：不渲染任务名标签', () => {
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    expect(screen.queryByTestId('panel-task-tab')).not.toBeInTheDocument();
  });
});

describe('PanelTabs · 新增/切换/关闭标签', () => {
  async function openPanelWithTerminal(): Promise<ReturnType<typeof userEvent.setup>> {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-terminal'));
    return user;
  }

  it('从「＋ 新增标签」选择器打开终端：标签出现并激活，body 渲染 TerminalPane（态 B）', async () => {
    await openPanelWithTerminal();
    expect(screen.getByTestId('panel-tab')).toHaveTextContent('终端');
    expect(screen.getByTestId('panel-tab')).toHaveAttribute('data-active', 'true');
    // 态 B：bash 子标签行 + TerminalView 只读回显
    expect(screen.getByTestId('terminal-pane')).toBeInTheDocument();
    expect(screen.getByTestId('terminal-pane-tab-root')).toHaveClass('on');
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
  });

  it('终端标签「关闭终端面板」钮 = 关闭当前标签（store closeTab）', async () => {
    const user = await openPanelWithTerminal();
    await user.click(screen.getByTestId('terminal-pane-close'));
    expect(useWorkbenchStore.getState().tabs).toHaveLength(0);
    expect(screen.queryByTestId('terminal-pane')).not.toBeInTheDocument();
  });

  it('占位标签（浏览器/辅助对话）：渲染结构化 PlaceholderPane（按类型给标题 + 说明）', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-browser'));
    expect(screen.getByTestId('panel-tab')).toHaveTextContent('浏览器');
    // 浏览器 = P2 线框（标签条灰角标 + 占位说明结构化）
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('浏览器 · P2 接入');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('browser_action');
    // 辅助对话：P1 结构化占位（message_delta 说明）
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-assist'));
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('辅助对话 · P1 接入');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('message_delta');
  });

  it('文件预览占位标签（file-preview:<path>）：分发 FilePreviewPane，其余占位仍走 PlaceholderPane', async () => {
    useWorkbenchStore.getState().setCollapsed(false);
    act(() => {
      useWorkbenchStore.getState().openTab({
        kind: 'placeholder',
        id: 'file-preview:obs-demo/README.md',
        title: 'README.md',
      });
    });
    render(<WorkbenchPanel />);
    // 文件预览 pane（.crumb 面包屑 + 占位正文）
    const pane = screen.getByTestId('file-preview-pane');
    expect(pane.textContent).toContain('root/obs-demo/README.md');
    expect(screen.queryByTestId('placeholder-pane')).not.toBeInTheDocument();

    // 其余 placeholder 标签不受影响（如辅助对话）
    act(() => {
      useWorkbenchStore.getState().openTab({ kind: 'placeholder', id: 'assist', title: '辅助对话' });
    });
    expect(screen.getByTestId('placeholder-pane')).toBeInTheDocument();
  });

  it('新增占位类型（源代码查看/完整计划/子智能体进度）：结构化占位态，说明按类型区分', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);

    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-code-view'));
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('源代码查看 · P1 接入');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('行号');

    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-plan'));
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('完整计划 · P1 接入');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('plan_ready');

    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-subagent'));
    const saTab = screen
      .getAllByTestId('panel-tab')
      .find((el) => el.textContent?.includes('子智能体进度'));
    expect(saTab).toBeDefined();
    expect(saTab).toHaveClass('sa-tab');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('子智能体进度 · M4 接入');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('task()');
  });

  it('多标签切换：激活标签的 body 跟随；关闭全部标签 → 面板空态 + 自动收起', async () => {
    const user = await openPanelWithTerminal();
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-assist'));
    const tabsAfterOpen = screen.getAllByTestId('panel-tab');
    expect(tabsAfterOpen).toHaveLength(2);
    // 新开者激活（assist 标签 data-active=true）
    const activeTab = tabsAfterOpen.find((el) => el.getAttribute('data-active') === 'true');
    expect(activeTab).toBeDefined();
    expect(activeTab?.textContent).toContain('辅助对话');

    // 切回终端标签（tab 内第一个按钮 = 切标签）
    const terminalTabEl = screen
      .getAllByTestId('panel-tab')
      .find((el) => el.textContent?.includes('终端'));
    if (terminalTabEl === undefined) throw new Error('终端标签缺失');
    await user.click(within(terminalTabEl).getAllByRole('button')[0] as HTMLElement);
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();

    // 关闭两个标签
    const tabsNow = screen.getAllByTestId('panel-tab');
    for (const tab of tabsNow) {
      const closeBtn = within(tab).getByTestId('panel-tab-close');
      await user.click(closeBtn);
    }
    expect(screen.queryAllByTestId('panel-tab')).toHaveLength(0);
    // 关闭全部标签 → store 自动收起（closeTab 语义），面板回到宽度 0
    expect(useWorkbenchStore.getState().collapsed).toBe(true);
    expect(screen.getByTestId('workbench-panel')).toHaveAttribute('data-collapsed', 'true');
  });
});

describe('审查标签（M3 Phase 4）：渲染真面板，不再是占位', () => {
  it('review 标签激活时渲染 DiffReviewPanel（摘要条 + 文件列表 + 裁决区）', async () => {
    // 种子：store 已有该 review（面板免 REST 回填）；fixture 备好文件详情（首个文件自动选中懒加载）
    const review = makeDiffReview({ id: 'rev-w1', session_id: 'sess-a' });
    useDiffStore.getState().upsertFromRest(review);
    setDiffReviewsFixture([review]);
    setDiffFilesFixture('rev-w1', [
      makeDiffFile({
        path: 'app.py',
        patch: [
          'diff --git a/app.py b/app.py',
          'index 64d908b..6912166 100644',
          '--- a/app.py',
          '+++ b/app.py',
          '@@ -1,2 +1,3 @@',
          ' import os',
          '+import sys',
          ' ',
        ].join('\n'),
      }),
    ]);

    useWorkbenchStore.getState().setCollapsed(false);
    useWorkbenchStore.getState().openReviewTab('rev-w1', '审查 · 第 1 轮');
    render(<WorkbenchPanel />);

    expect(screen.getByTestId('panel-tab')).toHaveTextContent('审查 · 第 1 轮');
    expect(screen.getByTestId('diff-review-panel')).toBeInTheDocument();
    expect(screen.getByTestId('diff-summary-bar')).toBeInTheDocument();
    expect(screen.getByTestId('diff-verdict-form')).toBeInTheDocument();
    // 不再是占位面板
    expect(screen.queryByTestId('placeholder-pane')).not.toBeInTheDocument();
  });
});
