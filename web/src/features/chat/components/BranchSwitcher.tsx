import { useState } from 'react';

import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';

/**
 * Git 分支切换器（F23；toolbar-menus 菜单 6 一比一；输入区上方 main 钮弹出）。
 *
 * 轻量 dialog：搜索分支 combobox + 「分支」组 + 「创建并检出新分支...」禁用态 +
 * 「Git 图谱」置灰（LCode P1 占位）。分支数据 P1 前无来源（沙箱内 git 策略未定，
 * RD#29/任务头 main 徽标同口径），本仓库按 main 静态占位；搜索框对占位列表
 * 做本地过滤（React 状态等价实现，不照搬原型脚本）。
 */

/** P1 前静态占位分支表（本仓库 main）。 */
const BRANCHES: readonly string[] = ['main'];

export function BranchSwitcher() {
  const [query, setQuery] = useState('');
  const keyword = query.trim();
  const visible = BRANCHES.filter((branch) => branch.includes(keyword));

  const items: MenuItem[] = [
    {
      key: 'search',
      kind: 'static',
      itemClassName: 'search',
      label: (
        <>
          <Icon name="search" size={12} className="text-foreground-subtlest" />
          <input
            data-testid="branch-search"
            aria-label="搜索分支"
            placeholder="搜索分支"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </>
      ),
    },
    { key: 'hdr-branches', kind: 'header', label: '分支' },
    ...visible.map((branch) => ({
      key: `branch-${branch}`,
      label: <span className="nm mono">{branch}</span>,
      checkFirst: true,
      selected: branch === 'main',
      onSelect: () => undefined, // 当前分支：选中即关闭（P1 前无切换动作）
    })),
    {
      key: 'create-branch',
      leading: <Icon name="plus" size={12} className="text-foreground-subtlest" />,
      label: '创建并检出新分支...',
      disabled: true,
      title: '分支操作 P1 占位（沙箱内 git 提交/分支策略未定）',
    },
    { key: 'sep-graph', kind: 'separator' },
    {
      key: 'git-graph',
      leading: <Icon name="git-graph" size={12} />,
      label: 'Git 图谱',
      disabled: true,
      title: 'LCode P1 占位：Git 图谱视图未排期',
    },
  ];

  return (
    <Menu
      trigger={
        <>
          <Icon name="branch" size={12} className="text-foreground-subtlest" />
          main
          <Icon name="chevron-down" size={10} className="chev" />
        </>
      }
      triggerClassName="proj-chip"
      triggerLabel="切换 Git 分支"
      triggerTestId="home-branch-chip"
      menuLabel="切换分支"
      menuMaxWidth={320}
      items={items}
    />
  );
}
