// @vitest-environment jsdom
/**
 * 成本可见（AC-015-2/3）：QuotaButton 环形钮 + 三档浮层（本会话/今日/按模型并发 3 请求）；
 * 无会话置灰；加载失败如实提示；SessionCostCard 两段（模型/沙箱）与格式函数。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { http, HttpResponse } from 'msw';

import { costByDateFixture, requestLog } from '../../../tests/msw-handlers';
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

describe('QuotaButton · 三档浮层（AC-015-2/3）', () => {
  it('无会话：置灰不可点（成本无归属）', () => {
    render(<QuotaButton sessionId={null} />);
    expect(screen.getByTestId('quota-button')).toBeDisabled();
  });

  it('点击打开浮层：并发 3 请求（会话成本 + date/model 两维汇总），三块数据齐备', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    render(<QuotaButton sessionId="sess-a" />);
    await user.click(screen.getByTestId('quota-button'));

    await waitFor(() => {
      expect(screen.getByTestId('session-cost-card')).toBeInTheDocument();
    });
    expect(screen.getByTestId('quota-by-date')).toHaveTextContent('今日成本');
    expect(screen.getByTestId('quota-date-total')).toHaveTextContent('¥1.23');
    const byModel = screen.getByTestId('quota-by-model');
    expect(within(byModel).getByText('mock/qwen/qwen3-flash')).toBeInTheDocument();
    expect(byModel).toHaveTextContent('¥0.90');
    expect(byModel).toHaveTextContent('¥0.33');
  });

  it('加载失败：如实提示暂不可用（不显示假数据）', async () => {
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
    expect(screen.queryByTestId('session-cost-card')).not.toBeInTheDocument();
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

  it('按模型中行为空时不渲染空块（不伪造行）', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('*/api/v1/costs/summary', ({ request }) => {
        const dimension = new URL(request.url).searchParams.get('dimension');
        if (dimension === 'model') {
          return HttpResponse.json({ code: '0', message: null, data: { dimension: 'model', rows: [] } });
        }
        return HttpResponse.json({ code: '0', message: null, data: costByDateFixture });
      }),
    );
    render(<QuotaButton sessionId="sess-a" />);
    await user.click(screen.getByTestId('quota-button'));
    await waitFor(() => expect(screen.getByTestId('session-cost-card')).toBeInTheDocument());
    expect(screen.queryByTestId('quota-by-model')).not.toBeInTheDocument();
  });
});
