import { useState, type ReactNode } from 'react';

import { patchSession } from '@/api/sessions';
import { ApiError } from '@/api/http';

import { useSessionStore } from '../store';

import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';

/**
 * 审批模式菜单（A4 §8.1「权限模式菜单」行；toolbar-menus 权限模式；AC-013-3/4）。
 * 归属 sessions 域：数据面是 SessionDetail.approval_mode + PATCH /sessions/{id}
 * （依赖方向 §2.2——sessions/components 引自己的 store 与 api，不跨 feature）。
 *
 * F6 命名核准（toolbar-menus 菜单 2 逐字）：计划模式（checkbox）/ 变更前确认[默认] /
 * 自主模式 / 完全访问。LCode 契约口径：四档是 MANUAL/AUTO 语义的呈现层命名映射，
 * 提交体契约不变——变更前确认 = MANUAL（默认），自主模式 = AUTO（安全非沙箱操作
 * 免审，"在有风险时询问"）；计划模式与完全访问 P0 契约无支撑，置灰占位呈现。
 *
 * - MANUAL/AUTO 切换选中即 PATCH /sessions/{id}（与模型热切换同模式：服务端回声
 *   为事实源）；沙箱红线常驻菜单内提示（AC-013-4）；切换自下一条消息生效（D5）；
 *   提交失败就地报错（ApiError 透传 code），不静默回滚。
 * - 两形态（variant）：header（默认）「审批：变更前确认 ▾」；composer（输入区工具条
 *   钮「切换模式：…」，R11）——激活自主模式时出现 tb-sep + 带 × 的 .pill-close，
 *   × 回默认模式；sessionId=null（首页）为受控模式（value/onChange），选中不触
 *   PATCH，建会话时随 SessionCreate.approval_mode 携带。
 */

/** 沙箱红线提示文案（AC-013-4 固定口径，单点常量供测试断言）。 */
export const SANDBOX_REDLINE_HINT =
  '沙箱内命令执行/文件写入始终需要人工审批，AUTO 不会放行它们';

/** F6 呈现层命名（toolbar-menus 菜单 2 逐字；提交体仍是 manual/auto）。 */
const MODE_LABEL = {
  manual: '变更前确认',
  auto: '自主模式',
} as const;

export interface ApprovalModeMenuProps {
  /** 当前会话 id；null = 首页（未建会话），走 value/onChange 受控模式（不触 PATCH） */
  readonly sessionId: string | null;
  /** 首页受控模式值（sessionId=null 时生效） */
  readonly value?: 'manual' | 'auto';
  /** 首页受控模式回调（sessionId=null 时生效） */
  readonly onChange?: (mode: 'manual' | 'auto') => void;
  /** header=会话头部钮（默认）；composer=输入区工具条钮（app-shell 原型形态） */
  readonly variant?: 'header' | 'composer';
}

export function ApprovalModeMenu({
  sessionId,
  value,
  onChange,
  variant = 'header',
}: ApprovalModeMenuProps) {
  const sessionMode = useSessionStore((s) =>
    sessionId === null ? undefined : (s.list.find((item) => item.id === sessionId)?.approval_mode ?? 'manual'),
  );
  const upsert = useSessionStore((s) => s.upsert);
  const [pendingMode, setPendingMode] = useState<'manual' | 'auto' | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** 当前生效模式：首页取受控值，会话内取 store（服务端回声为事实源）。 */
  const mode: 'manual' | 'auto' = sessionId === null ? (value ?? 'manual') : (sessionMode ?? 'manual');

  const switchMode = (next: 'manual' | 'auto'): void => {
    if (next === mode || pendingMode !== null) return;
    if (sessionId === null) {
      onChange?.(next); // 首页：只改本地选择，随 SessionCreate 携带
      return;
    }
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

  const modeLabel =
    pendingMode !== null && pendingMode !== mode ? '切换中…' : MODE_LABEL[mode];

  /** 单选圈前置图标（原型口径：选中档内圆填充）。 */
  const radioIcon = (on: boolean): ReactNode => (
    <Icon name={on ? 'radio-on' : 'radio'} size={13} className={on ? undefined : 'text-foreground-subtlest'} />
  );

  const menuItems: MenuItem[] = [
    {
      key: 'plan',
      leading: <Icon name="checkbox" size={13} className="text-foreground-subtlest" />,
      label: <span className="nm">计划模式</span>,
      desc: <span className="ds">编辑前先出计划。</span>,
      disabled: true,
      title: '计划模式（P1 参考：契约 v1 无 plan 族事件，占位呈现）',
    },
    { key: 'sep-modes', kind: 'separator' },
    {
      key: 'manual',
      leading: radioIcon(mode === 'manual'),
      label: <span className="nm">变更前确认</span>,
      desc: <span className="ds">改文件前先问我。</span>,
      selected: mode === 'manual',
      onSelect: () => switchMode('manual'),
    },
    {
      key: 'auto',
      leading: radioIcon(mode === 'auto'),
      label: <span className="nm">自主模式</span>,
      desc: <span className="ds">在有风险时询问</span>,
      selected: mode === 'auto',
      onSelect: () => switchMode('auto'),
    },
    {
      key: 'full',
      leading: <Icon name="radio" size={13} className="text-foreground-subtlest" />,
      label: <span className="nm">完全访问</span>,
      desc: <span className="ds">减少确认次数。</span>,
      disabled: true,
      title: '完全访问暂未开放（P0 契约仅变更前确认 / 自主模式两档）',
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
  ];

  if (variant === 'composer') {
    return (
      <div className="flex items-center">
        <Menu
          trigger={
            <>
              <Icon name="shield" size={13} />
              <span>切换模式：{modeLabel}</span>
              {/* 原型口径：.ic.chev（app-shell.html 切换模式钮） */}
              <Icon name="chevron-down" size={10} className="chev" />
            </>
          }
          triggerClassName="tool-btn"
          triggerLabel="切换模式"
          triggerTestId="composer-mode-menu"
          menuLabel="选择审批模式"
          direction="up"
          disabled={pendingMode !== null}
          items={menuItems}
        />
        {/* R11（RD#16）：激活非默认模式 → separator + 带 × 的 pill（app-shell.html） */}
        {mode === 'auto' && (
          <>
            <span className="tb-sep" aria-hidden="true" data-testid="composer-mode-sep" />
            <button
              type="button"
              className="pill-close"
              data-testid="composer-mode-pill"
              title="关闭自主模式（回到默认：变更前确认）"
              onClick={() => switchMode('manual')}
            >
              <span className="x" aria-hidden="true">
                <Icon name="close" size={9} />
              </span>
              关闭自主模式
              <span className="badge badge-accent">自主</span>
            </button>
          </>
        )}
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

  return (
    <div className="flex items-center gap-2">
      <Menu
        trigger={
          <>
            <span>审批：{modeLabel}</span>
            <span aria-hidden="true" className="text-foreground-subtlest">
              <Icon name="chevron-down" size={12} />
            </span>
          </>
        }
        triggerClassName="flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2.5 text-[length:var(--text-ui-xs)] text-foreground-subtle transition-colors hover:border-border-hover hover:text-foreground disabled:cursor-not-allowed"
        triggerLabel="审批模式"
        triggerTestId="approval-mode-menu"
        align="right"
        menuLabel="选择审批模式"
        disabled={pendingMode !== null}
        items={menuItems}
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
