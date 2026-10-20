import { useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';

import { sendMessage, stopRun } from '@/api/messages';
import { ApiError } from '@/api/http';
import type { MessageSendAck } from '@contracts/rest';
import { useChatStore } from './store';

/**
 * 输入区（A4 §8.1 Composer 行；AC-001-1/2/4）。
 *
 * - 发送/停止切换：run 未收敛（idle 之外）显示「停止生成」（AC-001-2，幂等 stop；
 *   已收内容由前端保留——停止不清空，run_status(stopped) 终态回 input 态）；
 * - 空输入轻提示不发请求（AC-001-4）；
 * - 两旋钮挂载点（AC-014-1）：`knobs` 插槽由上层组装（ModelSelector/EffortSelector
 *   等）——本组件不直接 import models feature（A4 §2.2 跨 feature 禁令，chat 的
 *   容器例外仅限 components/）；
 * - 等待审批期间发送禁用（审批是唯一闸门，REQ-013/R9）。
 */

export interface ComposerProps {
  /** 当前会话 id；null = 尚未建会话（HomeView），经 onSendWithoutSession 兜底 */
  sessionId: string | null;
  /** 工具条左区插槽（模型/档位两旋钮等） */
  knobs?: ReactNode;
  /** 工具条右区插槽（额度钮等，位于发送钮左侧） */
  trailing?: ReactNode;
  /** sessionId 为空时的兜底发送（HomeView：先建会话再发消息） */
  onSendWithoutSession?: (content: string) => Promise<void>;
  /** 发送 ack 回调（App 接 streamManager.ensureCaughtUp(ack.event_seq)，§4.2 路径 B） */
  onSent?: (ack: MessageSendAck) => void;
  placeholder?: string;
}

const DEFAULT_PLACEHOLDER = '向 LCode 提问，描述你的任务…';

export function Composer({
  sessionId,
  knobs,
  trailing,
  onSendWithoutSession,
  onSent,
  placeholder = DEFAULT_PLACEHOLDER,
}: ComposerProps) {
  const runState = useChatStore((s) => s.activeRun.state);
  const runId = useChatStore((s) => s.activeRun.runId);

  const [value, setValue] = useState('');
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [hint, setHint] = useState<string | null>(null);

  const trimmed = value.trim();
  const running = runState !== 'idle';
  const waitingApproval = runState === 'waiting_approval';
  const sendDisabled =
    sending ||
    running ||
    waitingApproval ||
    trimmed === '' ||
    (sessionId === null && onSendWithoutSession === undefined);

  const submit = async (): Promise<void> => {
    if (trimmed === '') {
      // AC-001-4：空输入不发请求，轻提示
      setHint('请输入内容后再发送');
      return;
    }
    if (running || waitingApproval || sending) return;
    setHint(null);
    setSending(true);
    try {
      if (sessionId === null) {
        if (onSendWithoutSession === undefined) return;
        await onSendWithoutSession(trimmed);
      } else {
        const ack = await sendMessage(sessionId, { content: trimmed });
        onSent?.(ack);
      }
      setValue('');
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.code === 'RUN_ALREADY_ACTIVE') setHint('已有运行中的任务，请先停止或稍候');
        else if (error.code === 'NETWORK_ERROR') setHint('网络错误，请稍后重试');
        else if (error.code === 'MESSAGE_EMPTY') setHint('请输入内容后再发送');
        else setHint(`发送失败（${error.code}）`);
      } else {
        setHint('发送失败，请重试');
      }
    } finally {
      setSending(false);
    }
  };

  const stop = async (): Promise<void> => {
    if (runId === null || stopping) return;
    setStopping(true);
    setHint(null);
    try {
      await stopRun(runId); // 幂等；已收内容由前端保留（AC-001-2）
    } catch (error) {
      if (error instanceof ApiError && error.code === 'RUN_NOT_ACTIVE') {
        // F12：run 已在服务端终态（终态事件迟到/丢失时停止会打到 409）——
        // 如实提示并收敛回可输入态，不误报「停止失败」
        setHint('该运行已结束');
        useChatStore.getState().markRunEnded();
      } else {
        setHint(error instanceof ApiError ? `停止失败（${error.code}）` : '停止失败，请重试');
      }
    } finally {
      setStopping(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.nativeEvent.isComposing) return; // IME 组合中不提交
    event.preventDefault();
    void submit();
  };

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    void submit();
  };

  return (
    <div className="flex-none px-5 pb-4">
      <form
        data-testid="composer"
        onSubmit={onSubmit}
        className="mx-auto max-w-[760px] rounded-xl border border-border bg-card transition-colors focus-within:border-accent-border"
      >
        <textarea
          data-testid="composer-input"
          aria-label="任务输入"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            if (hint !== null) setHint(null);
          }}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          rows={2}
          className="block w-full resize-none bg-transparent px-3.5 py-2.5 text-[length:var(--text-ui-sm)] leading-[1.6] text-foreground outline-none placeholder:text-foreground-subtlest"
        />
        <div className="flex flex-wrap items-center gap-1 px-2 pb-2">
          {knobs}
          <span className="flex-1" />
          {trailing}
          {running ? (
            <button
              type="button"
              data-testid="composer-stop"
              disabled={stopping}
              className="inline-flex h-[30px] items-center gap-1.5 rounded-md border border-destructive/45 bg-destructive/8 px-3 text-[length:var(--text-ui-xs)] text-destructive hover:bg-destructive/15 disabled:opacity-45"
              onClick={() => void stop()}
            >
              <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-[2px] bg-current" />
              {stopping ? '正在停止…' : '停止生成'}
            </button>
          ) : (
            <button
              type="submit"
              data-testid="composer-send"
              disabled={sendDisabled}
              title={sendDisabled && trimmed === '' ? '发送（空输入时禁用）' : '发送'}
              className="inline-flex h-[30px] w-[30px] items-center justify-center rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
            >
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 19V5M5 12l7-7 7 7" />
              </svg>
              <span className="sr-only">发送</span>
            </button>
          )}
        </div>
      </form>
      <div aria-live="polite" data-testid="composer-hint" className="mx-auto mt-1 min-h-[18px] max-w-[760px] text-center text-[length:var(--text-ui-2xs)] text-foreground-subtle">
        {waitingApproval ? '等待审批处理，暂不可发送' : (hint ?? '')}
      </div>
    </div>
  );
}
