import { useCallback, useEffect, useRef, useState } from 'react';

import { getCostSummary, getSessionCost } from '@/api/costs';
import type { CostSummary, SessionCost } from '@contracts/rest';

import { Spinner } from '@/components/Spinner';

import { SessionCostCard, formatCost } from './SessionCostCard';

/**
 * 剩余额度环形钮 + 三档浮层（A4 §8.1 QuotaButton 行；F22 / AC-015-2/3）。
 *
 * 浮层三块：本会话（GET /sessions/{id}/costs）+ 今日（summary dimension=date）+
 * 模型分摊（summary dimension=model）——并发 3 请求（A4 §12 未决 8 默认方案）。
 * 无会话（首页）时钮置灰。环形进度无配额总量语义（P2 计费前无真实 quota 端点），
 * 圆环仅作成本入口图标（不伪造百分比）。
 */
export interface QuotaButtonProps {
  sessionId?: string | null;
}

interface QuotaData {
  readonly sessionCost: SessionCost | null;
  readonly byDate: CostSummary | null;
  readonly byModel: CostSummary | null;
}

const IDLE = { sessionCost: null, byDate: null, byModel: null } as const;

export function QuotaButton({ sessionId }: QuotaButtonProps) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<QuotaData>(IDLE);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async (): Promise<void> => {
    if (sessionId == null) return;
    setLoading(true);
    setError(false);
    try {
      const [sessionCost, byDate, byModel] = await Promise.all([
        getSessionCost(sessionId),
        getCostSummary('date'),
        getCostSummary('model'),
      ]);
      setData({ sessionCost, byDate, byModel });
    } catch (err) {
      console.error('[costs] 成本数据加载失败：', err);
      setError(true);
    } finally {
      setLoading(false);
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

  const disabled = sessionId == null;

  return (
    <div ref={rootRef} className="relative inline-flex">
      <button
        type="button"
        data-testid="quota-button"
        aria-label="成本与额度"
        aria-expanded={open}
        disabled={disabled}
        title={disabled ? '成本（建会话后可用）' : '成本与额度（本会话/今日/模型）'}
        className="inline-flex h-[26px] w-[26px] flex-none items-center justify-center rounded-sm hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-45"
        onClick={() => setOpen((v) => !v)}
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
          aria-label="成本与额度"
          className="absolute bottom-[calc(100%+8px)] left-0 z-50 w-[300px] rounded-lg border border-border-hover bg-popover p-3 shadow-lg"
        >
          <p className="mb-2 text-[length:var(--text-ui-xs)] font-semibold text-foreground-strong">成本与额度</p>
          {loading && (
            <div className="flex items-center gap-2 py-2 text-foreground-subtle">
              <Spinner /> 加载中…
            </div>
          )}
          {!loading && error && (
            <p className="py-2 text-[length:var(--text-ui-xs)] text-destructive" role="alert">
              成本数据暂不可用，稍后重试。
            </p>
          )}
          {!loading && !error && (
            <div className="flex flex-col gap-2">
              {data.sessionCost !== null && <SessionCostCard cost={data.sessionCost} />}
              {data.byDate !== null && (
                <div data-testid="quota-by-date" className="rounded-md border border-border bg-card px-3 py-2 text-[length:var(--text-ui-xs)]">
                  <span className="text-foreground-subtle">今日成本 </span>
                  <span data-testid="quota-date-total" className="ml-auto font-mono">
                    {formatCost(sumRows(data.byDate))}
                  </span>
                </div>
              )}
              {data.byModel !== null && data.byModel.rows.length > 0 && (
                <div data-testid="quota-by-model" className="rounded-md border border-border bg-card px-3 py-2 text-[length:var(--text-ui-xs)]">
                  <p className="mb-1 text-foreground-subtle">按模型</p>
                  {data.byModel.rows.slice(0, 5).map((row) => (
                    <div key={row.group_key} className="flex items-center justify-between gap-2 py-0.5">
                      <span className="truncate font-mono text-foreground-subtlest">{row.group_key}</span>
                      <span className="flex-none font-mono">{formatCost(row.cost)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function sumRows(summary: CostSummary): number {
  return summary.rows.reduce((total, row) => total + row.cost, 0);
}
