import { Menu, type MenuItem } from '@/components/Menu';

/**
 * 斜杠命令选择器（/）（A4 §8.1 Composer「命令」钮；toolbar-menus 菜单 5 一比一）。
 *
 * 三段完整呈现：命令（/goal /workflow /compact /init /plan，官方描述已核
 * R12/snapshots §7）+ 技能（用户 / 插件两类 $ 前缀示例，RD#8）+ 子智能体
 * （LCode 内置 4 角色，带角色描述与 Tools 标注行）；底部提示 = 原型逐字。
 * P0 命令通道未接入前为静态演示：条目置灰（title 说明），点击不发送。
 */

/** 命令项（两行说明卡 .mi.cmd）。 */
function commandItem(key: string, command: string, desc: string): MenuItem {
  return {
    key,
    itemClassName: 'mi cmd disabled',
    label: (
      <>
        <span className="cmd-line">{command}</span>
        <span className="cmd-desc">{desc}</span>
      </>
    ),
    disabled: true,
    title: `${command}（P0 命令通道接入前为静态演示）`,
  };
}

export function SlashCommandMenu() {
  const items: MenuItem[] = [
    { key: 'hdr-commands', kind: 'header', label: '命令' },
    commandItem('cmd-goal', '/goal', 'Show or set the current session goal.'),
    commandItem('cmd-workflow', '/workflow', 'Design and launch a dynamic workflow for a task.'),
    commandItem('cmd-compact', '/compact', 'Compact the current conversation with optional instructions.'),
    commandItem('cmd-init', '/init', 'Create or update workspace AGENTS.md instructions.'),
    commandItem('cmd-plan', '/plan', 'Switch to Plan mode and optionally send a task.'),
    { key: 'sep-skills', kind: 'separator' },
    { key: 'hdr-skills', kind: 'header', label: '技能' },
    {
      key: 'skill-fetch-url',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">$fetch-url</span>
          <span className="cmd-desc">用户 · 抓取网页正文并消毒进上下文…</span>
        </>
      ),
      disabled: true,
      title: '技能示例（RD#8 LCode 语境占位；消毒硬约束 6）',
    },
    {
      key: 'skill-commit-msg',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">$commit-msg</span>
          <span className="cmd-desc">插件 · 按约定式提交生成 commit message…</span>
        </>
      ),
      disabled: true,
      title: '技能示例（RD#8 LCode 语境占位）',
    },
    { key: 'sep-agents', kind: 'separator' },
    { key: 'hdr-agents', kind: 'header', label: '子智能体（LCode 内置 4 角色）' },
    {
      key: 'agent-general-purpose',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">general-purpose</span>
          <span className="cmd-desc">
            通用任务执行单元，接手绝大多数委派工作。
            <span className="tools">Tools: 全部工具 · 审批</span>
          </span>
        </>
      ),
      disabled: true,
      title: '子智能体委派（deepagents task()，P0 事件通道内自然出现；此处为选择器演示）',
    },
    {
      key: 'agent-explorer',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">explorer</span>
          <span className="cmd-desc">
            只读探索，检索代码与文档，免审批快速定位。
            <span className="tools">Tools: 只读 7 工具 · 免审批</span>
          </span>
        </>
      ),
      disabled: true,
      title: '子智能体委派（deepagents task()，选择器演示）',
    },
    {
      key: 'agent-coder',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">coder</span>
          <span className="cmd-desc">
            全工具 + 沙箱执行，承担编码与运行验证。
            <span className="tools">Tools: 全部工具 + 沙箱 · 审批</span>
          </span>
        </>
      ),
      disabled: true,
      title: '子智能体委派（deepagents task()，选择器演示）',
    },
    {
      key: 'agent-reviewer',
      itemClassName: 'mi cmd disabled',
      label: (
        <>
          <span className="cmd-line">reviewer</span>
          <span className="cmd-desc">
            只读审查，输出结构化 findings 供上游消费。
            <span className="tools">Tools: 只读 7 工具 · 结构化输出</span>
          </span>
        </>
      ),
      disabled: true,
      title: '子智能体委派（deepagents task()，选择器演示）',
    },
    {
      key: 'foot',
      kind: 'foot',
      label: <div className="hint">输入内容以搜索命令、技能或子智能体</div>,
    },
  ];

  return (
    <Menu
      trigger={
        <>
          <span aria-hidden="true" className="font-mono">
            /
          </span>{' '}
          命令
        </>
      }
      triggerClassName="tool-btn"
      triggerLabel="命令"
      triggerTestId="composer-slash"
      menuLabel="命令与能力"
      direction="up"
      menuMaxWidth={560}
      items={items}
    />
  );
}
