import { useState } from 'react';

import { patchSession } from '@/api/sessions';
import { ApiError } from '@/api/http';

import { useSessionStore } from '../store';

import { Menu } from '@/components/Menu';

/**
 * 审批模式菜单（A4 §8.1「权限模式菜单」行；toolbar-menus 权限模式；AC-013-3/4）。
 * 归属 sessions 域：数据面是 SessionDetail.approval_mode + PATCH /sessions/{id}
 * （依赖方向 §2.2——sessions/components 引自己的 store 与 api，不跨 feature）。
 *
 * - MANUAL（默认，每次询问）/ AUTO（安全非沙箱操作免审）切换，选中即
 *   PATCH /sessions/{id}（与模型热切换同模式：服务端回声为事实源）；
 * - 沙箱红线常驻菜单内提示：沙箱内命令执行/文件写入始终需要人工审批，AUTO
 *   不会放行它们（AC-013-4；策略构造期保证，前端只做如实呈现）；
 * - 切换自下一条消息生效（运行中策略固定于 run 启动快照，D5）；
 * - 提交失败就地报错（ApiError 透传 code），不静默回滚——上一模式仍显示直至
 *   服务端回声覆盖。
 */

/** 沙箱红线提示文案（AC-013-4 固定口径，单点常量供测试断言）。 */
export const SANDBOX_REDLINE_HINT =
  '沙箱内命令执行/文件写入始终需要人工审批，AUTO 不会放行它们';

export interface ApprovalModeMenuProps {
  readonly sessionId: string;
}

export function ApprovalModeMenu({ sessionId }: ApprovalModeMenuProps) {
  const mode = useSessionStore(
    (s) => s.list.find((item) => item.id === sessionId)?.approval_mode ?? 'manual',
  );
  const upsert = useSessionStore((s) => s.upsert);
  const [pendingMode, setPendingMode] = useState<'manual' | 'auto' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const switchMode = (next: 'manual' | 'auto'): void => {
    if (next === mode || pendingMode !== null) return;
    setPendingMode(next);
    setError(null);
    patchSession(sessionId, { approval_mode: next })
      .then((echo) => {
        upsert(echo); // 服务端回声为事实源（下一条消息生效，D5）
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          setError(`审批模式切换失败（${err.code}）：${err.message}`);
        } else {
          setError(`审批模式切换失败（UNKNOWN）：${err instanceof Error ? err.message : String(err)}`);
        }
      })
      .finally(() => {
        setPendingMode(null);
      });
  };

  const modeLabel = pendingMode !== null && pendingMode !== mode ? '切换中…' : mode === 'auto' ? '自动' : '手动';

  return (
    <div className="flex items-center gap-2">
      <Menu
        trigger={
          <>
            <span>审批：{modeLabel}</span>
            <span aria-hidden="true" className="text-foreground-subtlest">
              ▾
            </span>
          </>
        }
        triggerClassName="flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2.5 text-[length:var(--text-ui-xs)] text-foreground-subtle transition-colors hover:border-border-hover hover:text-foreground disabled:cursor-not-allowed"
        triggerLabel="审批模式"
        triggerTestId="approval-mode-menu"
        align="right"
        menuLabel="选择审批模式"
        disabled={pendingMode !== null}
        items={[
          {
            key: 'manual',
            label: '手动审批',
            hint: '每次询问',
            selected: mode === 'manual',
            onSelect: () => switchMode('manual'),
          },
          {
            key: 'auto',
            label: '自动审批',
            hint: '安全操作免审',
            selected: mode === 'auto',
            onSelect: () => switchMode('auto'),
          },
          {
            key: 'redline',
            // 沙箱红线提示（AC-013-4）：禁用项只作说明，不可选中
            label: (
              <span className="block whitespace-normal text-[length:var(--text-ui-2xs)] leading-relaxed text-warning">
                {SANDBOX_REDLINE_HINT}
              </span>
            ),
            disabled: true,
          },
          {
            key: 'effective',
            label: (
              <span className="block whitespace-normal text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
                切换自下一条消息生效
              </span>
            ),
            disabled: true,
          },
        ]}
      />
      {error !== null && (
        <span
          data-testid="approval-mode-error"
          role="alert"
          className="max-w-[280px] truncate text-[length:var(--text-ui-2xs)] text-destructive"
          title={error}
        >
          {error}
        </span>
      )}
    </div>
  );
}
