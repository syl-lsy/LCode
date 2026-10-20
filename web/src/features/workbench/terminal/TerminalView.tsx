import { useEffect, useRef } from 'react';

import { useShallow } from 'zustand/react/shallow';

import { useTerminalStore, type TerminalLifecycle } from '../store';

import { EmptyState } from '@/components/EmptyState';

/**
 * 终端视图（A4 §8.1 TerminalView 行 / §6.3；AC-002-1）。
 *
 * 数据源 = terminalStore（sandbox_output stdout/stderr 经 TerminalBuffer 行缓冲
 * 100ms 限频 append；环形上限 2000 行，超出滚存并留 droppedLines 计数）。
 * 自研轻量回显：纯文本行 + exec_id 来源标注 + 退出码经工具卡展示（§6.3，不引入
 * xterm.js）。滚动跟随：用户在底部附近则自动跟随，上翻不打扰。
 */

/** 滚动跟随阈值（px，距底小于该值视为贴底）。 */
const FOLLOW_THRESHOLD_PX = 24;

function lifecycleLabel(phase: TerminalLifecycle['phase']): string {
  switch (phase) {
    case 'creating':
      return '沙箱创建中';
    case 'running':
      return '沙箱运行中';
    case 'paused':
      return '沙箱已暂停';
    case 'resumed':
    case 'recreated':
      return '沙箱运行中';
    case 'killed':
      return '沙箱已销毁';
    case 'crashed':
      return '沙箱异常退出';
    default:
      return phase;
  }
}

export function TerminalView() {
  const { execOrder, execs, lifecycle } = useTerminalStore(
    useShallow((s) => ({ execOrder: s.execOrder, execs: s.execs, lifecycle: s.lifecycle })),
  );

  const hasContent = execOrder.length > 0;

  return (
    <div data-testid="terminal-view" className="flex h-full min-h-0 flex-col">
      {lifecycle !== null && (
        <div
          data-testid="terminal-lifecycle"
          data-phase={lifecycle.phase}
          className="flex flex-none items-center gap-1.5 border-b border-border px-3 py-1 text-[length:var(--text-ui-2xs)] text-foreground-subtle"
        >
          <span
            aria-hidden="true"
            className={`h-[5px] w-[5px] flex-none rounded-full ${
              lifecycle.phase === 'paused' || lifecycle.phase === 'killed' || lifecycle.phase === 'crashed'
                ? 'bg-warning'
                : 'bg-success'
            }`}
          />
          {lifecycleLabel(lifecycle.phase)}
          {lifecycle.provider !== null && (
            <span className="font-mono text-foreground-subtlest">· {lifecycle.provider}</span>
          )}
          {lifecycle.reason !== null && (
            <span className="truncate text-foreground-subtlest" title={lifecycle.reason}>
              · {lifecycle.reason}
            </span>
          )}
        </div>
      )}
      {hasContent ? (
        <TerminalLines execOrder={execOrder} execs={execs} />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center">
          <EmptyState
            icon={<span aria-hidden="true">❯_</span>}
            title="终端 · 暂无输出"
            description="Agent 在沙箱中执行命令后，stdout/stderr 会在这里实时回显并标注来源命令。"
          />
        </div>
      )}
    </div>
  );
}

function TerminalLines({
  execOrder,
  execs,
}: {
  execOrder: readonly string[];
  execs: Map<string, { lines: readonly string[]; droppedLines: number; version: number }>;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const followRef = useRef(true);
  const versionsKey = execOrder
    .map((id) => `${id}:${execs.get(id)?.version ?? 0}`)
    .join('|');

  // 滚动跟随（§6.3：复用 §6.4 同一策略的轻量实现——底部附近则跟随，上翻不打扰）
  useEffect(() => {
    const el = containerRef.current;
    if (el === null) return;
    if (followRef.current) el.scrollTop = el.scrollHeight;
  }, [versionsKey]);

  const onScroll = (): void => {
    const el = containerRef.current;
    if (el === null) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    followRef.current = distance <= FOLLOW_THRESHOLD_PX;
  };

  return (
    <div
      ref={containerRef}
      onScroll={onScroll}
      data-testid="terminal-lines"
      className="scroll-thin min-h-0 flex-1 overflow-y-auto bg-terminal-bg px-3 py-2 font-mono text-[length:var(--code-font-size)] leading-[1.7] whitespace-pre-wrap break-all text-terminal-fg"
    >
      {execOrder.map((execId) => {
        const exec = execs.get(execId);
        if (exec === undefined) return null;
        return (
          <div key={execId} data-testid="terminal-exec" data-exec-id={execId}>
            <div
              data-testid="terminal-exec-label"
              className="mb-0.5 border-b border-border/60 pb-0.5 text-foreground-subtlest"
            >
              exec · {execId}
            </div>
            {exec.droppedLines > 0 && (
              <div data-testid="terminal-dropped" className="text-foreground-subtlest">
                （已滚存最早 {exec.droppedLines} 行）
              </div>
            )}
            {exec.lines.map((line, index) => (
              <div key={index} data-testid="terminal-line">
                {line === '' ? ' ' : line}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
