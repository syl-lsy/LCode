// @vitest-environment jsdom
/**
 * ToolCard（AC-003-2 两态卡片；R15 三卡型）：
 * running（spinner + 正在{类型}）/ 收口（成功打勾 | error 展示 code+message）；
 * exit_code 非 0 单独徽标；展开显示入参/出参；sandbox_unavailable 给重建提示（AC-002-4）。
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { ChatToolCardItem } from '../store';
import '../../../tests/test-utils';

import { ToolCard } from './ToolCard';

function card(overrides: Partial<ChatToolCardItem> = {}): ChatToolCardItem {
  return {
    kind: 'toolcard',
    id: 'call-1',
    tool: 'execute',
    args: { command: 'ls -la /root/obs-demo' },
    status: 'running',
    riskLevel: null,
    agent: null,
    createdAt: '2026-10-18T00:00:00+00:00',
    ...overrides,
  };
}

describe('ToolCard · 两态与三卡型', () => {
  it('running 态：正在终端 + spinner，摘要显示命令', () => {
    render(<ToolCard item={card()} />);
    const el = screen.getByTestId('tool-card');
    expect(el).toHaveAttribute('data-status', 'running');
    expect(el).toHaveTextContent('正在终端');
    expect(el).toHaveTextContent('ls -la /root/obs-demo');
  });

  it('成功态：打勾、无 spinner；点击展开出参预览与耗时', async () => {
    const user = userEvent.setup();
    render(
      <ToolCard
        item={card({
          status: 'success',
          durationMs: 120,
          exitCode: 0,
          resultPreview: 'total 8\nhello.py',
        })}
      />,
    );
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'success');
    expect(screen.queryByTestId('tool-card-detail')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('tool-card-toggle'));
    const detail = screen.getByTestId('tool-card-detail');
    expect(detail).toHaveTextContent('"command":"ls -la /root/obs-demo"');
    expect(detail).toHaveTextContent('total 8');
    expect(detail).toHaveTextContent('耗时 120 ms');
  });

  it('失败态：error code 徽标 + 错误详情原样（不美化）；sandbox_unavailable 追加重建提示', async () => {
    const user = userEvent.setup();
    render(
      <ToolCard
        item={card({
          id: 'call-2',
          tool: 'write_file',
          args: { path: 'hello.py' },
          status: 'error',
          error: { code: 'sandbox_unavailable', message: '沙箱实例不可用', retryable: true },
        })}
      />,
    );
    expect(screen.getByTestId('tool-error-code')).toHaveTextContent('sandbox_unavailable');
    await user.click(screen.getByTestId('tool-card-toggle'));
    expect(screen.getByTestId('tool-error-detail')).toHaveTextContent('沙箱实例不可用');
    expect(screen.getByTestId('tool-error-sandbox')).toHaveTextContent('运行已尝试重建或转报');
  });

  it('查阅卡（read_file）与写入卡（write_file）：标签随工具类型切换', () => {
    const view = render(<ToolCard item={card({ tool: 'read_file', args: { path: '/root/obs-demo' } })} />);
    expect(view.container.textContent).toContain('正在查阅');
    expect(view.container.textContent).toContain('/root/obs-demo');

    view.rerender(<ToolCard item={card({ tool: 'write_file', args: { path: 'hello.py' } })} />);
    expect(view.container.textContent).toContain('正在写入');
    expect(view.container.textContent).toContain('hello.py');
  });

  it('exit_code 非 0：exit 徽标；truncated：已截断徽标（诚实呈现）', () => {
    render(<ToolCard item={card({ status: 'success', durationMs: 5, exitCode: 127, truncated: true })} />);
    expect(screen.getByTestId('tool-exit-code')).toHaveTextContent('exit 127');
    expect(screen.getByTestId('tool-card')).toHaveTextContent('已截断');
  });

  it('状态切换（running → success）由 props 驱动原地更新，不换 id', () => {
    const view = render(<ToolCard item={card()} />);
    view.rerender(<ToolCard item={card({ status: 'success', durationMs: 9 })} />);
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-status', 'success');
    expect(view.container.querySelectorAll('[data-testid="tool-card"]')).toHaveLength(1);
  });
});
