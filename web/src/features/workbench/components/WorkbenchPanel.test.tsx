// @vitest-environment jsdom
/**
 * 右栏多用面板壳（A4 §9.1 / §8.1；R10）：
 * 收起（宽度 0）↔ 展开（220-480 拖拽）；标签条新增/切换/关闭；终端标签渲染 TerminalView；
 * 占位标签渲染 PlaceholderPane（P1 置灰空态）；无标签时给面板空态而非空白。
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { PANEL_WIDTH_MAX, PANEL_WIDTH_MIN, useDiffStore, useWorkbenchStore } from '../store';
import '../../../tests/test-utils';

import { makeDiffFile, makeDiffReview, setDiffFilesFixture, setDiffReviewsFixture } from '../../../tests/msw-handlers';

import { WorkbenchPanel } from './WorkbenchPanel';

describe('WorkbenchPanel · 收起/展开与拖拽（R10）', () => {
  it('默认收起：宽度 0、aria-hidden、不渲染内容', () => {
    render(<WorkbenchPanel />);
    const panel = screen.getByTestId('workbench-panel');
    expect(panel).toHaveAttribute('data-collapsed', 'true');
    expect(panel).toHaveClass('w-0');
    expect(screen.queryByTestId('panel-tabs')).not.toBeInTheDocument();
  });

  it('展开：按 store 宽度渲染标签条 + 无标签时面板空态', () => {
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    const panel = screen.getByTestId('workbench-panel');
    expect(panel).toHaveAttribute('data-collapsed', 'false');
    expect(panel).toHaveStyle({ width: `${useWorkbenchStore.getState().width}px` });
    expect(screen.getByTestId('panel-tabs')).toBeInTheDocument();
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('侧边面板');
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

describe('PanelTabs · 新增/切换/关闭标签', () => {
  async function openPanelWithTerminal(): Promise<ReturnType<typeof userEvent.setup>> {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-terminal'));
    return user;
  }

  it('从「＋ 新增标签」选择器打开终端：标签出现并激活，body 渲染 TerminalView', async () => {
    await openPanelWithTerminal();
    expect(screen.getByTestId('panel-tab')).toHaveTextContent('终端');
    expect(screen.getByTestId('panel-tab')).toHaveAttribute('data-active', 'true');
    expect(screen.getByTestId('terminal-view')).toBeInTheDocument();
  });

  it('占位标签（浏览器/辅助对话）：渲染 PlaceholderPane（P1 置灰空态，不伪造功能）', async () => {
    const user = userEvent.setup();
    useWorkbenchStore.getState().setCollapsed(false);
    render(<WorkbenchPanel />);
    await user.click(screen.getByTestId('panel-add-tab'));
    await user.click(screen.getByTestId('panel-chooser-browser'));
    expect(screen.getByTestId('panel-tab')).toHaveTextContent('浏览器');
    expect(screen.getByTestId('placeholder-pane')).toHaveTextContent('浏览器 · P1 接入');
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
