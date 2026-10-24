import { useCallback, useEffect, useRef, useState } from 'react';

import { getCostSummary, getSessionCost } from '@/api/costs';
import type { CostSummary, SessionCost } from '@contracts/rest';

import { Icon } from '@/components/Icon';

import { formatCost } from './SessionCostCard';

/**
 * 剩余额度环形钮 + 三档浮层（A4 §8.1 QuotaButton 行；F22 / AC-015-2/3）。
 *
 * F22 结构（toolbar-menus 浮层一比一）：头部 pill-green（本月预算剩余 64% 演示文案）
 * + 「更多 >」占位 + 三档额度卡 = 本会话成本 / 今日累计 / 本月累计（REQ-015 AC-015-3
 * 会话成本口径；P2 对接供应商配额前不做 5 小时/每周/MCP 官方结构）。
 *
 * 数据：本会话（GET /sessions/{id}/costs）+ 今日（summary dimension=date）并发 2 请求
 * （模型分摊块随 F22 三卡结构移除，A4 §12 未决 8 的备选不再采用）；本月累计无后端
 * 汇总端点，为静态演示卡。进度条分母为演示预算（P2 计费前无真实 quota 端点，圆环
 * 仅作成本入口图标不伪造百分比）。
 * 交互：官方行为 = 悬停出浮层（点击无浮层）；此处悬停展开 + 点击为键盘路径，
 * 移出浮层范围 / 外部点击关闭。无会话（首页）时钮置灰。
 */

/** 演示预算分母与本月演示卡（P2 计费前为演示件）。 */
const DEMO_BUDGET = { session: 2, date: 8 } as const;
const DEMO_MONTH = { used: 42.6, budget: 120, pct: 36, reset: '11 月 1 日重置' } as const;

function percent(used: number, budget: number): number {
  return Math.min(100, Math.round((used / budget) * 100));
}

export interface QuotaButtonProps {
  sessionId?: string | null;
}

interface QuotaData {
  readonly sessionCost: SessionCost | null;
  readonly byDate: CostSummary | null;
}

const IDLE = { sessionCost: null, byDate: null } as const;

export function QuotaButton({ sessionId }: QuotaButtonProps) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<QuotaData>(IDLE);
  const [error, setError] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const disabled = sessionId == null;

  const load = useCallback(async (): Promise<void> => {
    if (sessionId == null) return;
    setError(false);
    try {
      const [sessionCost, byDate] = await Promise.all([
        getSessionCost(sessionId),
        getCostSummary('date'),
      ]);
      setData({ sessionCost, byDate });
    } catch (err) {
      console.error('[costs] 成本数据加载失败：', err);
      setError(true);
    }
  }, [sessionId]);

  useEffect(() => {
    if (open) void load();
    else setData(IDLE);
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent): void => {
      if (rootRef.current !== null && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  const sessionTotal =
    data.sessionCost === null ? 0.42 : data.sessionCost.model_cost + data.sessionCost.sandbox.accrued;
  const sessionPct = percent(sessionTotal, DEMO_BUDGET.session);
  const dateTotal = data.byDate === null ? 3.18 : data.byDate.rows.reduce((total, row) => total + row.cost, 0);
  const datePct = percent(dateTotal, DEMO_BUDGET.date);

  return (
    /* F22：悬停出浮层（官方行为），移出范围关闭；点击为键盘路径 */
    <div
      ref={rootRef}
      className="relative inline-flex"
      onMouseEnter={() => {
        if (!disabled) setOpen(true);
      }}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        data-testid="quota-button"
        aria-label="剩余额度"
        aria-expanded={open}
        disabled={disabled}
        title={disabled ? '剩余额度（建会话后可用）' : '剩余额度（悬停或点击展开）'}
        className="quota-ring disabled:cursor-not-allowed disabled:opacity-45"
        onClick={() => setOpen(true)}
      >
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="9" stroke="var(--color-quota-track)" strokeWidth="3" />
          <circle
            cx="12"
            cy="12"
            r="9"
            stroke="var(--color-quota-2)"
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray={open ? '18.8 37.7' : '56.5 56.5'}
            transform="rotate(-90 12 12)"
          />
        </svg>
      </button>
      {open && (
        <div
          data-testid="quota-popover"
          role="dialog"
          aria-label="剩余额度"
          /* .menu（components.css，toolbar-menus 浮层容器）+ 向上弹出 */
          className="menu absolute bottom-[calc(100%+8px)] left-0 z-50 w-[300px]"
        >
          <div className="ov-head">
            <b>剩余额度</b>
            {/* 演示 pill（P2 计费前无真实预算；文案语境化照原型） */}
            <span className="pill-green" data-testid="quota-pill">
              <Icon name="gift" size={11} /> 本月预算剩余 64%
            </span>
            <button
              type="button"
              className="more"
              data-testid="quota-more"
              disabled
              title="设置 · 使用统计子页（P1 占位）"
            >
              更多 &gt;
            </button>
          </div>
          <div className="ov-body">
            {error && (
              <p className="mb-1 text-[length:var(--text-ui-2xs)] text-destructive" role="alert">
                成本数据暂不可用，以下为演示数据。
              </p>
            )}
            <div className="qcard" data-testid="quota-card-session">
              <div className="qt">
                本会话成本
                <span className="qv" data-testid="quota-session-value">
                  {formatCost(sessionTotal)} / {formatCost(DEMO_BUDGET.session)}
                </span>
              </div>
              <div className="qbar">
                <i
                  data-testid="quota-bar-session"
                  style={{ width: `${sessionPct}%`, background: 'var(--color-quota-1)' }}
                />
              </div>
              <div className="qr">
                <span>随会话结束归档结算</span>
                <span className="mono">{sessionPct}%</span>
              </div>
            </div>
            <div className="qcard" data-testid="quota-card-date">
              <div className="qt">
                今日累计
                <span className="qv" data-testid="quota-date-total">
                  {formatCost(dateTotal)} / {formatCost(DEMO_BUDGET.date)}
                </span>
              </div>
              <div className="qbar">
                <i
                  data-testid="quota-bar-date"
                  style={{ width: `${datePct}%`, background: 'var(--color-quota-2)' }}
                />
              </div>
              <div className="qr">
                <span>次日 00:00 重置</span>
                <span className="mono">{datePct}%</span>
              </div>
            </div>
            <div className="qcard" data-testid="quota-card-month">
              <div className="qt">
                本月累计
                <span className="qv" data-testid="quota-month-value">
                  {formatCost(DEMO_MONTH.used)} / {formatCost(DEMO_MONTH.budget)}
                </span>
              </div>
              <div className="qbar">
                <i
                  data-testid="quota-bar-month"
                  style={{ width: `${DEMO_MONTH.pct}%`, background: 'var(--color-quota-3)' }}
                />
              </div>
              <div className="qr">
                <span>{DEMO_MONTH.reset}</span>
                <span className="mono">{DEMO_MONTH.pct}%</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
