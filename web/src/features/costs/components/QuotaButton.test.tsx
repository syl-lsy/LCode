// @vitest-environment jsdom
/**
 * 成本可见（AC-015-2/3）：QuotaButton 环形钮 + F22 三档浮层（本会话/今日真实数据 +
 * 本月静态演示卡，进度条分母为演示预算）；悬停展开（官方行为）+ 外部点击关闭；
 * 无会话置灰；加载失败如实提示并回退演示数据；SessionCostCard 两段与格式函数。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { http, HttpResponse } from 'msw';

import { server } from '../../../tests/test-utils';

import { QuotaButton } from './QuotaButton';
import { SessionCostCard, formatCost, formatTokens } from './SessionCostCard';

describe('SessionCostCard · 两段成本与格式', () => {
  it('模型段（token + 费用）与沙箱段（累计 + 小时费率）分列', () => {
    render(
      <SessionCostCard
        cost={{
          model: { input_tokens: 1200, output_tokens: 340 },
          model_cost: 0.42,
          sandbox: { accrued: 0.16, currency: 'CNY', rate_per_hour: 0.156 },
        }}
      />,
    );
    expect(screen.getByTestId('cost-total')).toHaveTextContent('¥0.42');
    expect(screen.getByTestId('cost-model')).toHaveTextContent('1,540 tokens');
    expect(screen.getByTestId('cost-sandbox')).toHaveTextContent('¥0.16');
    expect(screen.getByTestId('cost-sandbox')).toHaveTextContent('¥0.16/h');
  });

  it('formatCost/formatTokens：货币符号与千分位；未知货币不加符号', () => {
    expect(formatCost(0.156)).toBe('¥0.16');
    expect(formatCost(1.5, 'USD')).toBe('$1.50');
    expect(formatCost(2, 'JPY')).toBe('2.00');
    expect(formatTokens({ input_tokens: 52340, output_tokens: 8210 })).toBe('60,550');
  });
});

describe('QuotaButton · F22 三档浮层（AC-015-2/3）', () => {
  it('无会话：置灰不可点（成本无归属）', () => {
    render(<QuotaButton sessionId={null} />);
    expect(screen.getByTestId('quota-button')).toBeDisabled();
  });

  it('点击打开浮层：头部演示 pill + 更多占位 + 三档额度卡；本会话/今日为真实数据', async () => {
    const user = userEvent.setup();
    render(<QuotaButton sessionId="sess-a" />);
    await user.click(screen.getByTestId('quota-button'));

    // 头部（F22）：pill-green 演示文案 + 「更多 >」占位
    const popover = screen.getByTestId('quota-popover');
    expect(within(popover).getByTestId('quota-pill')).toHaveTextContent('本月预算剩余 64%');
    expect(within(popover).getByTestId('quota-more')).toBeDisabled();

    // 并发 2 请求（会话成本 + 今日汇总；MSW 未 mock 请求即红，数据到达即证请求发生）
    await waitFor(() => {
      expect(screen.getByTestId('quota-session-value')).toHaveTextContent('¥0.58');
    });
    expect(screen.getByTestId('quota-session-value')).toHaveTextContent('/ ¥2.00');
    expect(screen.getByTestId('quota-card-session')).toHaveTextContent('随会话结束归档结算');
    expect(screen.getByTestId('quota-date-total')).toHaveTextContent('¥1.23');
    expect(screen.getByTestId('quota-date-total')).toHaveTextContent('/ ¥8.00');
    expect(screen.getByTestId('quota-card-date')).toHaveTextContent('次日 00:00 重置');
    // 三档进度条：蓝/绿/橙（quota-1/2/3；jsdom 不解析 var()，按 style 属性原文断言）
    expect(screen.getByTestId('quota-bar-session').getAttribute('style')).toContain('var(--color-quota-1)');
    expect(screen.getByTestId('quota-bar-date').getAttribute('style')).toContain('var(--color-quota-2)');
    expect(screen.getByTestId('quota-bar-month').getAttribute('style')).toContain('var(--color-quota-3)');
  });

  it('本月累计为静态演示卡（无后端汇总端点，P2 对接供应商配额前）', async () => {
    const user = userEvent.setup();
    render(<QuotaButton sessionId="sess-a" />);
    await user.click(screen.getByTestId('quota-button'));
    const month = await screen.findByTestId('quota-card-month');
    expect(month).toHaveTextContent('¥42.60 / ¥120.00');
    expect(month).toHaveTextContent('11 月 1 日重置');
    expect(month).toHaveTextContent('36%');
  });

  it('加载失败：如实提示暂不可用，浮层回退演示数据（不空白）', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('*/api/v1/sessions/:id/costs', () =>
        HttpResponse.json({ code: 'INTERNAL_ERROR', message: 'boom' }, { status: 500 }),
      ),
    );
    render(<QuotaButton sessionId="sess-a" />);
    await user.click(screen.getByTestId('quota-button'));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('成本数据暂不可用');
    });
    // 回退演示数据（本会话 ¥0.42 / ¥2.00 · 21%）
    expect(screen.getByTestId('quota-session-value')).toHaveTextContent('¥0.42 / ¥2.00');
  });

  it('外部点击关闭浮层（关闭后清空缓存数据，重新打开重取）', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <QuotaButton sessionId="sess-a" />
        <button type="button" data-testid="outside">
          外部
        </button>
      </div>,
    );
    await user.click(screen.getByTestId('quota-button'));
    await waitFor(() => expect(screen.getByTestId('quota-popover')).toBeInTheDocument());
    await user.click(screen.getByTestId('outside'));
    expect(screen.queryByTestId('quota-popover')).not.toBeInTheDocument();
  });

  it('悬停展开 / 移出范围关闭（F22 官方悬停行为）', () => {
    render(
      <div>
        <QuotaButton sessionId="sess-a" />
        <button type="button" data-testid="outside">
          外部
        </button>
      </div>,
    );
    expect(screen.queryByTestId('quota-popover')).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByTestId('quota-button'));
    expect(screen.getByTestId('quota-popover')).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByTestId('quota-button'));
    expect(screen.queryByTestId('quota-popover')).not.toBeInTheDocument();
  });
});
