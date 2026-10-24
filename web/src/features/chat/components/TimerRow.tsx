import { useEffect, useState } from 'react';

import { Icon } from '@/components/Icon';

/**
 * 计时器行（A4 §8.1 TimerRow 行；F3 = 中间步骤总折叠开关）。
 *
 * 运行中 = spinner + 「工作中 N 秒」；收口 = 打勾 + 「已工作 M 分 S 秒」。
 * 点击整行切换折叠：收起时行文案不变，思考块+工具卡整体藏入该行（R3§D）；
 * chevron 随折叠态旋转（.timer.collapsed → -90deg，原型同款）。
 */

function formatElapsed(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds === 0 ? `${minutes} 分` : `${minutes} 分 ${seconds} 秒`;
}

function secondsSince(startIso: string, nowMs: number): number {
  const start = Date.parse(startIso);
  if (!Number.isFinite(start)) return 0;
  return Math.max(0, Math.floor((nowMs - start) / 1000));
}

export interface TimerRowProps {
  running: boolean;
  /** 计时基准（组内首条卡片/消息的 created_at） */
  startedAt: string | null;
  collapsed: boolean;
  onToggle: () => void;
}

export function TimerRow({ running, startedAt, collapsed, onToggle }: TimerRowProps) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const elapsed = startedAt === null ? 0 : secondsSince(startedAt, nowMs);
  const label = running ? `工作中 ${formatElapsed(elapsed)}` : `已工作 ${formatElapsed(elapsed)}`;

  return (
    <button
      type="button"
      data-testid="timer-row"
      aria-expanded={!collapsed}
      onClick={onToggle}
      /* .timer.tgl（task-conversation.css，来源 task-conversation.html F3 计时器行） */
      className={`timer tgl ${collapsed ? 'collapsed' : ''}`}
      title="点击收起/展开中间步骤（思考块+工具卡）"
    >
      {running ? (
        <span className="spinner flex-none" aria-hidden="true" />
      ) : (
        <Icon name="check" size={12} className="text-success" />
      )}
      {label}
      <span className="chev" aria-hidden="true">
        <Icon name="chevron-down" size={10} />
      </span>
    </button>
  );
}
