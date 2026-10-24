import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';

/**
 * 帮助菜单（F24；toolbar-menus 菜单 7 一比一；引用方：app-shell rail、
 * task-conversation 头部帮助钮）。
 *
 * 两分组：产品向（产品文档/用户社群/问题上报/提交漏洞/给产品提需求）｜
 * 应用向（导出日志/在 Finder 中打开[置灰]/检查更新/关于 LCode）——条目逐字照
 * 原型；Web 形态无落地页/桌面语义，条目全部置灰占位（title 说明，RD#9 Finder
 * 本地化口径延续）。
 */

export interface HelpMenuProps {
  /** 触发按钮 data-testid（rail / 头部两个挂载点区分用） */
  readonly triggerTestId?: string;
}

export function HelpMenu({ triggerTestId = 'help-menu' }: HelpMenuProps) {
  const items: MenuItem[] = [
    { key: 'docs', label: '产品文档', disabled: true, title: '产品文档（P1 占位）' },
    { key: 'community', label: '用户社群', disabled: true, title: '用户社群（P1 占位）' },
    { key: 'issue', label: '问题上报', disabled: true, title: '问题上报（P1 占位）' },
    { key: 'vuln', label: '提交漏洞', disabled: true, title: '提交漏洞（P1 占位）' },
    { key: 'request', label: '给产品提需求', disabled: true, title: '给产品提需求（P1 占位）' },
    { key: 'sep-app', kind: 'separator' },
    { key: 'export-logs', label: '导出日志', disabled: true, title: '导出日志（P1 占位）' },
    {
      key: 'finder',
      label: '在 Finder 中打开',
      disabled: true,
      title: 'Web 形态无本地文件系统语义，P2 桌面壳启用',
    },
    { key: 'update', label: '检查更新', disabled: true, title: '检查更新（P2 桌面壳语义，Web 占位）' },
    { key: 'about', label: '关于 LCode', disabled: true, title: '关于 LCode（P1 占位）' },
  ];

  return (
    <Menu
      trigger={<Icon name="help" size={15} />}
      triggerClassName="icon-btn"
      triggerLabel="帮助"
      triggerTestId={triggerTestId}
      menuLabel="帮助"
      menuMaxWidth={280}
      items={items}
    />
  );
}
