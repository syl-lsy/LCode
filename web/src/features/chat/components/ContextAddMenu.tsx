import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';

/**
 * 添加上下文菜单（@）（A4 §8.1 Composer「添加上下文」钮；toolbar-menus 菜单 4 一比一）。
 *
 * 三段结构：添加（附件/目标/工作流）+ 插件（$ 前缀 LCode 语境占位，RD#8）+
 * 文件（搜索中行）——P1 上下文引用接入前为静态演示：条目置灰（title 说明），
 * 网页内容进上下文前必须消毒（AGENTS.md 硬约束 6，$fetch-url 描述如实标注）。
 * 底部 menu-foot 提示 = 原型逐字。
 */

export function ContextAddMenu() {
  const items: MenuItem[] = [
    { key: 'hdr-add', kind: 'header', label: '添加' },
    { key: 'attachment', label: '附件', disabled: true, title: '附件引用（P1 占位）' },
    { key: 'goal', label: '目标', disabled: true, title: '目标引用（/goal 运行态 P1 接入，占位）' },
    { key: 'workflow', label: '工作流', disabled: true, title: '工作流引用（P2 占位）' },
    { key: 'sep-plugins', kind: 'separator' },
    { key: 'hdr-plugins', kind: 'header', label: '插件' },
    {
      key: 'fetch-url',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">$fetch-url</span>
          <span className="cmd-desc">插件 · 抓取网页正文并消毒进上下文（RD#8 LCode 占位）</span>
        </>
      ),
      disabled: true,
      title: '技能示例（RD#8 LCode 语境占位；消毒硬约束 6）',
    },
    {
      key: 'sandbox-files',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">$sandbox-files</span>
          <span className="cmd-desc">插件 · 打包上传本地文件到沙箱数据面</span>
        </>
      ),
      disabled: true,
      title: '技能示例（RD#8 LCode 语境占位）',
    },
    {
      key: 'searching-files',
      kind: 'static',
      itemClassName: 'searching',
      label: (
        <>
          <span className="spinner" aria-hidden="true" />
          文件
          <span className="mono ml-auto">搜索中...</span>
        </>
      ),
    },
    {
      key: 'foot',
      kind: 'foot',
      label: (
        <>
          <div className="hints">
            <span className="hint">
              <span className="kbd">@</span> 添加上下文
            </span>
            <span className="hint">
              <span className="kbd">/</span> 选择能力
            </span>
            <span className="hint">
              <span className="kbd">$</span> 选择技能
            </span>
          </div>
          <div className="hint">输入内容以搜索插件、文件和对话</div>
        </>
      ),
    },
  ];

  return (
    <Menu
      trigger={
        <>
          <Icon name="plus" size={13} /> 添加上下文
        </>
      }
      triggerClassName="tool-btn"
      triggerLabel="添加上下文"
      triggerTestId="composer-context"
      menuLabel="添加上下文"
      direction="up"
      items={items}
    />
  );
}
