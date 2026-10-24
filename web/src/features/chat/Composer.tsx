import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';

import { sendMessage, stopRun } from '@/api/messages';
import { ApiError } from '@/api/http';
import type { MessageSendAck } from '@contracts/rest';
import { Icon } from '@/components/Icon';
import { ContextAddMenu } from './components/ContextAddMenu';
import { SlashCommandMenu } from './components/SlashCommandMenu';
import { useChatStore } from './store';

/**
 * 输入区（A4 §8.1 Composer 行；AC-001-1/2/4；原型 app-shell 输入区 1:1）。
 *
 * - 发送/停止切换：run 未收敛（idle 之外）显示「停止生成」（AC-001-2，幂等 stop；
 *   已收内容由前端保留——停止不清空，run_status(stopped) 终态回 input 态）；
 * - 空输入轻提示不发请求（AC-001-4）；
 * - 两旋钮挂载点（AC-014-1）：`knobs` 插槽由上层组装（ModelSelector/EffortSelector
 *   等）——本组件不直接 import models feature（A4 §2.2 跨 feature 禁令，chat 的
 *   容器例外仅限 components/）；`mode` 插槽同理由上层组装（ApprovalModeMenu
 *   composer 变体），位于占位钮之后、knobs 之前（原型工具条顺序）；
 * - `trailing` 插槽：发送钮左侧（QuotaButton 等额度钮）；
 * - 快捷卡注入（prefill）：外部把待填文本以 {text,id} 传入，id 变化即并入输入框；
 * - 添加上下文（@）/命令（/）= toolbar-menus 菜单 4/5 静态演示菜单（B5，条目置灰
 *   占位，P1 通道接入前不发送）；
 * - 等待审批期间发送禁用（审批是唯一闸门，REQ-013/R9）。
 */

export interface ComposerProps {
  /** 当前会话 id；null = 尚未建会话（HomeView），经 onSendWithoutSession 兜底 */
  sessionId: string | null;
  /** 工具条左区插槽（模型/档位两旋钮等，位于 mode 插槽之后） */
  knobs?: ReactNode;
  /** 模式插槽（ApprovalModeMenu composer 变体 + R11 pill，上层组装） */
  mode?: ReactNode;
  /** 工具条右区插槽（额度钮等，位于发送钮左侧） */
  trailing?: ReactNode;
  /** sessionId 为空时的兜底发送（HomeView：先建会话再发消息） */
  onSendWithoutSession?: (content: string) => Promise<void>;
  /** 发送 ack 回调（App 接 streamManager.ensureCaughtUp(ack.event_seq)，§4.2 路径 B） */
  onSent?: (ack: MessageSendAck) => void;
  placeholder?: string;
  /** 快捷卡注入：id 变化时把 text 并入输入框（空则直接填，非空则换行追加） */
  prefill?: { readonly text: string; readonly id: number } | null;
}

const DEFAULT_PLACEHOLDER = '向 LCode 提问，描述你的任务…';

export function Composer({
  sessionId,
  knobs,
  mode,
  trailing,
  onSendWithoutSession,
  onSent,
  placeholder = DEFAULT_PLACEHOLDER,
  prefill = null,
}: ComposerProps) {
  const runState = useChatStore((s) => s.activeRun.state);
  const runId = useChatStore((s) => s.activeRun.runId);

  const [value, setValue] = useState('');
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const lastPrefillIdRef = useRef<number | null>(null);

  // 快捷卡注入（app-shell .quick 的 React 等价实现）：id 变化即并入输入框并聚焦
  // （按 id 去重——父组件重渲染产生的新对象不触发重复注入）
  useEffect(() => {
    if (prefill === null || prefill.id === lastPrefillIdRef.current) return;
    lastPrefillIdRef.current = prefill.id;
    setValue((prev) => (prev.trim() === '' ? prefill.text : `${prev}\n${prefill.text}`));
    setHint(null);
    inputRef.current?.focus();
  }, [prefill]);

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
    <div className="flex-none">
      <form
        data-testid="composer"
        onSubmit={onSubmit}
        /* .composer（components.css，来源 app-shell.html）：圆角卡 + focus-within 描边 */
        className="composer"
      >
        <textarea
          data-testid="composer-input"
          ref={inputRef}
          aria-label="任务输入"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            if (hint !== null) setHint(null);
          }}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          rows={2}
          /* .composer-input（components.css）给尺寸/字号；此处只补 textarea 基线 */
          className="composer-input block w-full resize-none bg-transparent text-foreground outline-none placeholder:text-foreground-subtlest"
        />
        <div className="composer-bar">
          {/* 添加上下文（@ 入口）：toolbar-menus 菜单 4 静态演示（B5） */}
          <ContextAddMenu />
          {/* 斜杠命令（/ 入口）：toolbar-menus 菜单 5 静态演示（B5） */}
          <SlashCommandMenu />
          {mode}
          {knobs}
          {/* .bar-spacer（app-shell.css，来源 app-shell.html） */}
          <span className="bar-spacer" aria-hidden="true" />
          {trailing}
          {running ? (
            <button
              type="button"
              data-testid="composer-stop"
              disabled={stopping}
              /* .stop（task-conversation.css，来源 task-conversation.html 停止钮） */
              className="stop"
              onClick={() => void stop()}
            >
              <Icon name="stop" size={12} />
              {stopping ? '正在停止…' : '停止生成'}
            </button>
          ) : (
            /* .send（components.css，来源 app-shell.html 发送钮）；禁用态走全局 button:disabled */
            <button
              type="submit"
              data-testid="composer-send"
              disabled={sendDisabled}
              title={sendDisabled && trimmed === '' ? '发送（空输入时禁用）' : '发送'}
              className="send"
            >
              <Icon name="send" size={15} />
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
