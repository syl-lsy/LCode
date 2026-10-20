import { useEffect, useRef, useState } from 'react';

/**
 * 左下错误/断线 toast（F28，A4 §8.1「Toast（断线/错误）」）。
 *
 * 数据源 = connStore 的连接状态镜像——但 components/ 是纯展示层（A4 §2.2 依赖方向：
 * 不读 store），状态由 AppShell 订阅后以 props 注入。
 *
 * 语义（AC-003-3 / NFR-006）：reconnecting = 「连接中断，正在重连」（持续展示）；
 * offline = 「连接已断开」（可手动关闭）；从断开态恢复到 open 后短暂展示「连接已恢复」
 * 再自动隐藏；首次即 open（从未断过）不展示。
 */
export type ToastConnState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'offline' | 'closed';

export interface ToastProps {
  state: ToastConnState;
  onDismiss?: () => void;
}

/** 「连接已恢复」提示自动消失延迟（ms）。 */
export const TOAST_RECOVER_MS = 3000;

const DISCONNECTED: ReadonlySet<ToastConnState> = new Set(['reconnecting', 'offline']);

export function Toast({ state, onDismiss }: ToastProps) {
  const [showRecovered, setShowRecovered] = useState(false);
  const prevStateRef = useRef<ToastConnState>(state);

  // 断开态 → open 的恢复沿才提示；首次即 open（从未断过）不展示
  useEffect(() => {
    const prev = prevStateRef.current;
    prevStateRef.current = state;
    if (state !== 'open' || !DISCONNECTED.has(prev)) {
      setShowRecovered(false);
      return;
    }
    setShowRecovered(true);
    const timer = window.setTimeout(() => setShowRecovered(false), TOAST_RECOVER_MS);
    return () => window.clearTimeout(timer);
  }, [state]);

  if (state === 'reconnecting') {
    return (
      <div
        role="alert"
        data-testid="toast-conn"
        className="fixed bottom-3 left-3 z-[60] flex items-center gap-2 rounded-md border border-border-hover bg-toast px-3 py-2 text-[length:var(--text-ui-xs)] text-toast-foreground shadow-md"
      >
        <span className="spinner flex-none" aria-hidden="true" />
        连接中断，正在重连…
      </div>
    );
  }

  if (state === 'offline') {
    return (
      <div
        role="alert"
        data-testid="toast-conn"
        className="fixed bottom-3 left-3 z-[60] flex items-center gap-2 rounded-md border border-destructive bg-toast px-3 py-2 text-[length:var(--text-ui-xs)] text-toast-foreground shadow-md"
      >
        连接已断开（会话不存在或服务不可达）
        {onDismiss !== undefined && (
          <button
            type="button"
            data-testid="toast-dismiss"
            aria-label="关闭提示"
            className="ml-1 rounded-full px-1.5 hover:bg-surface-hover"
            onClick={onDismiss}
          >
            ×
          </button>
        )}
      </div>
    );
  }

  if (showRecovered) {
    return (
      <div
        role="status"
        data-testid="toast-conn"
        className="fixed bottom-3 left-3 z-[60] flex items-center gap-2 rounded-md border border-border-hover bg-toast px-3 py-2 text-[length:var(--text-ui-xs)] text-toast-foreground shadow-md"
      >
        连接已恢复
      </div>
    );
  }

  return null;
}
