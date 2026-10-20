import { useChatStore } from '../store';

/**
 * run 错误横幅（B2 / M2 收口 S4；AC-004-4 失败如实呈现）：
 * run_status phase=error 终局后，把 activeRun.error（契约 RunErrorObj：code/message/
 * retryable）呈现到聊天区顶部——message 正文 + code 徽标 + retryable=true 时「可重试」提示。
 *
 * 渲染条件 = `activeRun.error !== null`：store 语义里 error 与 finished/stopped 同为
 * run 终态、统一收敛 idle 并保留 error（ActiveRunState 无 'error' 值），error 非空即
 * 「run 处于 error 终态」。纯 store 驱动、无本地持久：新 run started / 后续终态事件
 * （finished/stopped 防御路径）/ reset 使 error 归 null 后横幅自动消失；已收敛 idle 时
 * markRunEnded 幂等不清 error（store 既有语义，横幅保留）。只消费状态，不写 store。
 */
export function RunErrorBanner() {
  const error = useChatStore((s) => s.activeRun.error);

  if (error === null) return null;

  return (
    <div data-testid="run-error-banner" className="flex-none px-5 pt-2">
      <div
        role="alert"
        className="mx-auto flex max-w-[760px] items-start gap-2 rounded-md border border-destructive/40 bg-card px-3 py-2"
      >
        <span aria-hidden="true" className="flex-none text-[length:var(--text-ui-xs)] leading-[1.6] text-destructive">
          ✕
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="flex-none text-[length:var(--text-ui-xs)] font-semibold text-destructive">
              运行失败
            </span>
            <span
              data-testid="run-error-code"
              className="badge flex-none border-destructive/40 text-destructive"
            >
              {error.code}
            </span>
            {error.retryable === true && (
              <span data-testid="run-error-retryable" className="badge flex-none" title="该错误可重试，重新发送即可">
                可重试
              </span>
            )}
          </div>
          <p
            data-testid="run-error-message"
            className="mt-0.5 text-[length:var(--text-ui-xs)] leading-[1.6] break-words text-foreground"
          >
            {error.message}
          </p>
        </div>
      </div>
    </div>
  );
}
