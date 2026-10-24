import { useEffect, useRef, useState, type ReactNode } from 'react';

import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';
import { useUiStore } from '@/stores/uiStore';

/**
 * 账号菜单（F16；toolbar-menus 菜单 8 一比一；Sidebar 账号钮点击展开）。
 *
 * 结构：头像 + 用户名 + ID（点击复制真实可用，1.2s「已复制 ✓」反馈）+
 * 三个快捷子菜单（界面语言[占位 F30 未实测] / 界面主题[真实可切，uiStore 驱动
 * html.dark 类] / 界面模式[占位]）+ 使用统计/升级/邀请好友/断开连接（P1/P2 置灰）。
 * 无「界面缩放」子菜单（RD#1 修订延续：Web 无应用级缩放）；Pro 徽标 P2 前不展示。
 * 鉴权 P2 前用户为单人本地占位（lcode / 演示 ID）。
 */

/** 鉴权 P2 前的占位账号（R1 修正口径：头像字母 L + lcode）。 */
const ACCOUNT = { name: 'lcode', id: '7f3a-9c2e-11d4' } as const;

/** 子菜单父行（原型 .mi.sub-parent，非交互静态行）。 */
function subParent(key: string, label: string): MenuItem {
  return {
    key,
    kind: 'static',
    itemClassName: 'mi sub-parent',
    label: (
      <>
        {label}
        <Icon name="chevron-right" size={12} className="arw" />
      </>
    ),
  };
}

export function AccountMenu() {
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);
  const [copied, setCopied] = useState(false);
  const copiedTimerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
    },
    [],
  );

  const copyId = (): void => {
    // 复制真实可用（原型 F16 脚本的 React 等价实现）；剪贴板不可用时仍给就地反馈
    if (typeof navigator !== 'undefined' && navigator.clipboard !== undefined) {
      navigator.clipboard.writeText(ACCOUNT.id).catch(() => undefined);
    }
    setCopied(true);
    if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = window.setTimeout(() => setCopied(false), 1200);
  };

  const themeItem = (key: string, label: string, selected: boolean, onPick: () => void): ReactNode => (
    <button
      key={key}
      type="button"
      role="menuitemradio"
      aria-checked={selected}
      data-testid={`account-theme-${key}`}
      className={`mi${selected ? ' sel' : ''}`}
      onClick={onPick}
    >
      {label}
      {selected && <Icon name="check" size={12} className="ck" />}
    </button>
  );

  const items: MenuItem[] = [
    {
      key: 'acct',
      itemClassName: 'acct',
      label: (
        <>
          <span className="av" aria-hidden="true">
            L
          </span>
          <span className="min-w-0 flex-1 text-left">
            <span className="an">{ACCOUNT.name}</span>{' '}
            <span className="aid" data-testid="account-id">
              {copied ? '已复制 ✓' : `ID: ${ACCOUNT.id}`}
            </span>
          </span>
        </>
      ),
      onSelect: copyId,
      keepOpen: true, // 原型 F16 口径：复制反馈就地呈现，菜单不关
      title: '点击复制用户 ID',
    },
    { key: 'sep-lang', kind: 'separator' },
    subParent('lang', '界面语言'),
    subParent('theme', '界面主题'),
    {
      key: 'theme-sub',
      kind: 'static',
      itemClassName: 'subwrap',
      label: (
        <>
          {themeItem('dark', '深色主题', theme === 'dark', () => setTheme('dark'))}
          {themeItem('light', '浅色主题', theme === 'light', () => setTheme('light'))}
          {themeItem('system', '跟随系统', false, () => {
            const prefersDark =
              typeof window !== 'undefined' &&
              typeof window.matchMedia === 'function' &&
              window.matchMedia('(prefers-color-scheme: dark)').matches;
            setTheme(prefersDark ? 'dark' : 'light');
          })}
        </>
      ),
    },
    subParent('mode', '界面模式'),
    {
      key: 'mode-sub',
      kind: 'static',
      itemClassName: 'subwrap',
      label: (
        <>
          <button
            type="button"
            data-testid="account-mode-coding"
            className="mi sel disabled"
            disabled
            title="当前模式（办公模式 P2 前不可切换）"
          >
            编程模式
            <Icon name="check" size={12} className="ck" />
          </button>
          <button
            type="button"
            data-testid="account-mode-office"
            className="mi disabled"
            disabled
            title="P2 多人部署后启用"
          >
            办公模式
          </button>
        </>
      ),
    },
    { key: 'sep-account', kind: 'separator' },
    { key: 'usage', label: '使用统计', disabled: true, title: '设置 · 使用统计子页（P1 占位）' },
    { key: 'upgrade', label: '升级', disabled: true, title: 'P2 多人部署后启用' },
    {
      key: 'invite',
      label: (
        <>
          邀请好友 <span className="badge">奖励</span>
        </>
      ),
      disabled: true,
      title: 'P2 多人部署后启用',
    },
    { key: 'sep-conn', kind: 'separator' },
    { key: 'disconnect', label: '断开连接', disabled: true, title: '鉴权 P2 前无连接语义' },
  ];

  return (
    <Menu
      trigger={
        <>
          <span className="avatar" aria-hidden="true">
            L
          </span>
          <span className="nm">lcode</span>
        </>
      }
      triggerClassName="account"
      triggerLabel="账号"
      triggerTestId="account-button"
      menuLabel="账号"
      direction="up"
      align="left"
      menuMaxWidth={300}
      rootClassName="w-full"
      items={items}
    />
  );
}
