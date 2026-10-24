// @vitest-environment jsdom
/**
 * 新手引导页 3 步静态演示（B7，对照 design/prototype/onboarding.html）：
 * - 默认第 1 步：三段进度条 aria + 12 项双列选项卡（默认勾选首项，单选互斥）；
 * - 翻步：进度条 aria-valuenow/aria-label 与 seg on/done 类随步切换；
 * - 第 2 步双选卡互斥；第 3 步占位文案；
 * - 「完成」写 localStorage「已完成引导」并回首页；「退出引导」回首页。
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { renderAtRoute } from '@/tests/test-utils';

describe('OnboardingView · 新手引导 3 步（B7）', () => {
  it('默认第 1 步：进度条 aria + 12 项选项卡默认勾选「软件开发/数据/AI」', () => {
    renderAtRoute('/onboarding');
    const prog = screen.getByTestId('onboarding-progress');
    expect(prog).toHaveAttribute('aria-valuenow', '1');
    expect(prog).toHaveAttribute('aria-label', '引导进度：第 1 步 · 工作方向');
    expect(prog.querySelector('.seg.on')).not.toBeNull();
    expect(prog.querySelector('.seg.done')).toBeNull();

    expect(screen.getByText('你的主要工作方向是？')).toBeInTheDocument();
    expect(screen.getByTestId('onboarding-jobs').querySelectorAll('button')).toHaveLength(12);
    expect(screen.getByRole('button', { name: '软件开发/数据/AI' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '其他职业' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('第 1 步选项卡单选互斥：点「学生/教师/科研」后默认项取消勾选', async () => {
    const user = userEvent.setup();
    renderAtRoute('/onboarding');
    await user.click(screen.getByRole('button', { name: '学生/教师/科研' }));
    expect(screen.getByRole('button', { name: '学生/教师/科研' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '软件开发/数据/AI' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('翻步：进度条 aria/seg 类随步切换；第 2 步双选卡互斥；上一步可回', async () => {
    const user = userEvent.setup();
    renderAtRoute('/onboarding');
    await user.click(within(screen.getByTestId('onboarding-step-1')).getByRole('button', { name: '下一步' }));

    expect(screen.getByText('你偏好哪种界面模式？')).toBeInTheDocument();
    const prog = screen.getByTestId('onboarding-progress');
    expect(prog).toHaveAttribute('aria-valuenow', '2');
    expect(prog).toHaveAttribute('aria-label', '引导进度：第 2 步 · UI 模式');
    expect(prog.querySelectorAll('.seg.done')).toHaveLength(1);
    expect(prog.querySelector('.seg.on')).toHaveAttribute('title', '第 2 步 · UI 模式');

    expect(screen.getByRole('button', { name: /编程模式/ })).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByRole('button', { name: /办公模式/ }));
    expect(screen.getByRole('button', { name: /办公模式/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /编程模式/ })).toHaveAttribute('aria-pressed', 'false');

    await user.click(within(screen.getByTestId('onboarding-step-2')).getByRole('button', { name: '上一步' }));
    expect(prog).toHaveAttribute('aria-valuenow', '1');
    expect(screen.getByText('你的主要工作方向是？')).toBeInTheDocument();
  });

  it('第 1 步「跳过」直达第 2 步', async () => {
    const user = userEvent.setup();
    renderAtRoute('/onboarding');
    await user.click(within(screen.getByTestId('onboarding-step-1')).getByRole('button', { name: '跳过' }));
    expect(screen.getByText('你偏好哪种界面模式？')).toBeInTheDocument();
  });

  it('第 3 步占位文案；「完成」写 localStorage 并回首页', async () => {
    const user = userEvent.setup();
    renderAtRoute('/onboarding');
    await user.click(within(screen.getByTestId('onboarding-step-1')).getByRole('button', { name: '下一步' }));
    await user.click(within(screen.getByTestId('onboarding-step-2')).getByRole('button', { name: '下一步' }));

    const prog = screen.getByTestId('onboarding-progress');
    expect(prog).toHaveAttribute('aria-valuenow', '3');
    expect(prog.querySelectorAll('.seg.done')).toHaveLength(2);
    expect(screen.getByText('选择你的工作助手偏好')).toBeInTheDocument();
    expect(screen.getByText('步骤 3 · 助手偏好（占位）')).toBeInTheDocument();

    await user.click(screen.getByTestId('onboarding-finish'));
    expect(localStorage.getItem('lcode.onboarding.done')).toBe('1');
    // 完成即回首页（首页侧不强制跳转，导航仅由本页完成钮触发）
    await waitFor(() => {
      expect(screen.getByTestId('home-greet')).toBeInTheDocument();
    });
  });

  it('「退出引导」回首页（不写完成标记）', async () => {
    const user = userEvent.setup();
    renderAtRoute('/onboarding');
    await user.click(screen.getByTestId('onboarding-exit'));
    expect(localStorage.getItem('lcode.onboarding.done')).toBeNull();
    await waitFor(() => {
      expect(screen.getByTestId('home-greet')).toBeInTheDocument();
    });
  });
});
