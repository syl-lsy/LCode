import type { SessionCost } from '@contracts/rest';
import type { Usage } from '@contracts/common';

/**
 * 会话成本卡（A4 §8.1 SessionCostCard 行；AC-015-3）。
 *
 * 三段呈现：模型段（token + 费用）+ 沙箱段（运行/暂停费用估算，AC-015-4；
 * 1C2G ≈ 0.156 元/h 口径由服务端 rate_per_hour 给出，前端只展示不换算）。
 * 纯展示组件：数据由调用方注入（QuotaButton 浮层 / 侧栏挂载点）。
 */

/** 成本金额格式（元；两位小数，货币符号跟随后端 currency 字段缺省 ¥）。 */
export function formatCost(amount: number, currency = 'CNY'): string {
  const symbol = currency === 'CNY' ? '¥' : currency === 'USD' ? '$' : '';
  return `${symbol}${amount.toFixed(2)}`;
}

/** token 数千分位（input+output 合计；口径：思考 token 已计入 output）。 */
export function formatTokens(usage: Usage): string {
  const input = usage.input_tokens ?? 0;
  const output = usage.output_tokens ?? 0;
  return (input + output).toLocaleString('zh-CN');
}

export interface SessionCostCardProps {
  cost: SessionCost;
}

export function SessionCostCard({ cost }: SessionCostCardProps) {
  return (
    <div data-testid="session-cost-card" className="rounded-md border border-border bg-card px-3 py-2 text-[length:var(--text-ui-xs)]">
      <div className="mb-1 flex items-center gap-2">
        <span className="font-medium text-foreground-strong">本会话成本</span>
        <span data-testid="cost-total" className="ml-auto font-mono text-foreground">
          {formatCost(cost.model_cost, cost.sandbox.currency)}
        </span>
      </div>
      <div className="flex items-center justify-between gap-3 text-foreground-subtle">
        <span data-testid="cost-model">
          模型 {formatTokens(cost.model)} tokens · {formatCost(cost.model_cost)}
        </span>
        <span data-testid="cost-sandbox">
          沙箱 {formatCost(cost.sandbox.accrued, cost.sandbox.currency)}
          <span className="text-foreground-subtlest">
            {' '}
            （{formatCost(cost.sandbox.rate_per_hour, cost.sandbox.currency)}/h）
          </span>
        </span>
      </div>
    </div>
  );
}
