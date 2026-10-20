import { useState } from 'react';
import { useNavigate } from 'react-router';

import { deleteSession } from '@/api/sessions';
import type { SessionDetail } from '@contracts/rest';

/**
 * 会话条目（AC-004-3 / F11）：
 * - 最近活跃时间相对化（刚刚 / N 分钟前 / 当日 HH:mm / MM-DD HH:mm）；
 * - 运行中 = 光点发光（--glow-running）；等待确认 = 绿色「等待确认」文字徽标（F11）；
 * - 删除（kill 沙箱联动，AC-002-3）：DELETE 后回调查里 upsert/remove；当前会话被删
 *   → 回调方导航回首页。
 */

/** 相对时间（nowMs 可注入供测试）。 */
export function formatRelativeTime(iso: string, nowMs: number = Date.now()): string {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const diffMs = nowMs - at;
  if (diffMs < 60_000) return '刚刚';
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`;
  const date = new Date(at);
  const now = new Date(nowMs);
  const hhmm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  if (sameDay) return hhmm;
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${hhmm}`;
}

/** 标题兜底（A4 §12 未决 10：自动标题生成前显示「新会话」+ 时间戳）。 */
function titleOf(session: SessionDetail): string {
  if (session.title.trim() !== '') return session.title;
  return `新会话 · ${formatRelativeTime(session.created_at)}`;
}

export interface SessionItemProps {
  session: SessionDetail;
  active: boolean;
  /** 删除完成回调（Sidebar 决定导航/清 current） */
  onDeleted?: (id: string) => void;
}

export function SessionItem({ session, active, onDeleted }: SessionItemProps) {
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);

  const remove = async (): Promise<void> => {
    if (deleting) return;
    setDeleting(true);
    try {
      await deleteSession(session.id);
      onDeleted?.(session.id);
    } catch (error) {
      console.error('[sessions] 删除会话失败：', error);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div
      data-testid="session-item"
      data-session-id={session.id}
      data-run-state={session.run_state}
      className={`group relative flex h-8 items-center gap-1.5 rounded-sm pr-1 pl-2.5 ${
        active ? 'bg-selected text-foreground-strong' : 'text-foreground hover:bg-surface-hover'
      }`}
    >
      {session.run_state === 'running' && (
        <span
          data-testid="session-running-dot"
          aria-label="运行中"
          className="pulse-dot h-[5px] w-[5px] flex-none rounded-full bg-brand"
          style={{ boxShadow: 'var(--glow-running)' }}
        />
      )}
      {session.run_state !== 'running' && <span className="w-[5px] flex-none" aria-hidden="true" />}
      <button
        type="button"
        data-testid="session-open"
        aria-current={active ? 'true' : undefined}
        className="min-w-0 flex-1 truncate text-left text-[length:var(--text-ui-sm)]"
        onClick={() => navigate(`/s/${session.id}`)}
        title={titleOf(session)}
      >
        {titleOf(session)}
      </button>
      {session.run_state === 'waiting_approval' && (
        <span
          data-testid="session-await-badge"
          className="flex-none rounded-full px-1.5 py-0.5 text-[length:var(--text-ui-2xs)]"
          style={{ color: 'var(--color-await-badge)', background: 'var(--color-await-badge-bg)' }}
        >
          等待确认
        </span>
      )}
      <span className="flex-none font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        {formatRelativeTime(session.last_active_at)}
      </span>
      <button
        type="button"
        data-testid="session-delete"
        aria-label={`删除会话 ${titleOf(session)}`}
        disabled={deleting}
        className="flex h-5 w-5 flex-none items-center justify-center rounded-sm text-foreground-subtlest opacity-0 hover:bg-surface-hover hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-45"
        onClick={() => void remove()}
      >
        ×
      </button>
    </div>
  );
}
