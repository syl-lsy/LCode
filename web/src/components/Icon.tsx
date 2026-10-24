import type { ReactElement, ReactNode } from 'react';

/**
 * 统一描边 SVG 图标组件（B0 基座，A4 §2.1 components/）。
 *
 * - 描边规范对齐原型 `.ic`（design/prototype/tokens.css）：fill none /
 *   stroke currentColor / stroke-width 1.7 / round cap+join，24px viewBox；
 *   尺寸经内联 style 注入（可覆盖 `.ic` 等类宽高，与原型 style 内联口径一致）。
 * - path 事实源 = design/prototype/*.html 页内内联 SVG（path 原样抄录）；
 *   原型没有的图标画最简描边版（下方逐条标注）。
 * - emoji/字符图标一律经本组件替换（B0 范围：侧栏工具条/会话条目/Composer/
 *   壳层折叠钮/搜索框/两旋钮触发器）。
 */

export type IconName =
  | 'chevron-up'
  | 'chevron-down'
  | 'chevron-left'
  | 'chevron-right'
  | 'copy'
  | 'pencil'
  | 'refresh'
  | 'search'
  | 'send'
  | 'stop'
  | 'terminal'
  | 'sun'
  | 'moon'
  | 'font-size'
  | 'more'
  | 'close'
  | 'collapse'
  | 'expand'
  | 'shield'
  | 'diff-add'
  | 'diff-remove'
  | 'branch'
  | 'panel'
  | 'plus'
  | 'help'
  | 'clock'
  | 'package'
  | 'mobile'
  | 'folder'
  | 'folder-plus'
  | 'archive'
  | 'filter'
  | 'move'
  | 'move-h'
  | 'gift'
  | 'file-text'
  | 'pulse'
  | 'present'
  | 'globe'
  | 'review-check'
  | 'settings'
  | 'check'
  | 'thumbs-up'
  | 'thumbs-down'
  | 'fork'
  | 'hook'
  | 'share'
  | 'sliders'
  | 'user'
  | 'wrap'
  | 'triangle-alert'
  | 'undo'
  | 'file-plus'
  | 'message-square'
  | 'code'
  | 'checkbox'
  | 'radio'
  | 'radio-on'
  | 'git-graph'
  | 'star'
  | 'bell'
  | 'link'
  | 'chart'
  | 'list'
  | 'search-minus'
  | 'sparkle'
  | 'panel-left'
  | 'key';

/** 24px viewBox 下的图形节点；注释标 path 来源页。（导出仅供测试枚举图标名） */
export const GLYPHS: Record<IconName, ReactNode> = {
  // chevron-down ← app-shell.html（.tool-btn/.proj-chip 的 .chev 原文）
  'chevron-down': <path d="M6 9l6 6 6-6" />,
  // chevron-up/left/right ← chevron-down 的镜像（原型无独立定义，最简描边版）
  'chevron-up': <path d="M18 15l-6-6-6 6" />,
  'chevron-left': <path d="M15 18l-6-6 6-6" />,
  'chevron-right': <path d="M9 18l6-6-6-6" />,
  // copy ← 最简描边版（原型无）
  copy: (
    <>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </>
  ),
  // pencil ← 最简描边版（原型无）
  pencil: <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />,
  // refresh ← app-shell.html（审查标签刷新钮原文）
  refresh: <path d="M21 12a9 9 0 1 1-2.6-6.4L21 8M21 3v5h-5" />,
  // search ← app-shell.html（侧栏搜索/面板搜索原文）
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </>
  ),
  // send ← app-shell.html（发送钮原文）
  send: <path d="M12 19V5M5 12l7-7 7 7" />,
  // stop ← 最简描边版（原型无；运行中停止钮）
  stop: <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" />,
  // terminal ← app-shell.html（终端钮原文）
  terminal: <path d="M4 17l6-5-6-5M12 19h8" />,
  // sun ← app-shell.html（设置钮日芒形原文，兼作主题切换日标）
  sun: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" />
    </>
  ),
  // moon ← app-shell.html（闲时任务月牙原文）
  moon: <path d="M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z" />,
  // font-size ← 最简描边版「A」字字形（原型无；界面字号钮）
  'font-size': <path d="M4 7V4h16v3M9 20h6M12 4v16" />,
  // more ← 横向三点（实心圆点；描边 h.01 圆点在本尺寸不可见，特例用 fill）
  more: (
    <>
      <circle cx="5" cy="12" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="19" cy="12" r="1.3" fill="currentColor" stroke="none" />
    </>
  ),
  // close ← app-shell.html（关闭 ×原文）
  close: <path d="M6 6l12 12M18 6L6 18" />,
  // collapse ← app-shell.html（「收起全部」双 chevron 向下原文）
  collapse: <path d="M7 6l5 5 5-5M7 13l5 5 5-5" />,
  // expand ← collapse 镜像（原型无独立定义，最简描边版）
  expand: <path d="M7 11l5-5 5 5M7 18l5-5 5 5" />,
  // shield ← app-shell.html（切换模式盾形原文）
  shield: <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" />,
  // diff-add ← app-shell.html（新建任务 + 号原文）
  'diff-add': <path d="M12 5v14M5 12h14" />,
  // diff-remove ← diff-add 的减号线（最简描边版）
  'diff-remove': <path d="M5 12h14" />,
  // branch ← app-shell.html（Git 分支钮原文）
  branch: (
    <>
      <circle cx="6" cy="6" r="2.6" />
      <circle cx="6" cy="18" r="2.6" />
      <circle cx="18" cy="6" r="2.6" />
      <path d="M6 8.6v6.8M18 8.6a9 9 0 0 1-9 9" />
    </>
  ),
  // panel ← app-shell.html（展开/收起侧边面板原文）
  panel: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M15 4v16" />
    </>
  ),
  // plus ← app-shell.html「+」号别名（与 diff-add 同 path，语义独立命名）
  plus: <path d="M12 5v14M5 12h14" />,
  // help ← app-shell.html（rail 帮助钮原文）
  help: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9a2.5 2.5 0 1 1 3.4 2.3c-.8.3-.9 1-.9 1.7M12 17h.01" />
    </>
  ),
  // clock ← app-shell.html（侧栏「自动化」钮原文）
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  // package ← app-shell.html（侧栏「插件市场」钮原文）
  package: (
    <>
      <path d="M21 8l-9-5-9 5v8l9 5 9-5V8z" />
      <path d="M3 8l9 5 9-5M12 13v8" />
    </>
  ),
  // mobile ← app-shell.html（侧栏「移动端远程控制」钮原文）
  mobile: (
    <>
      <rect x="7" y="2" width="10" height="20" rx="2" />
      <path d="M11 18h2" />
    </>
  ),
  // folder ← app-shell.html（项目分区/选择项目 chip 原文）
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />,
  // folder-plus ← app-shell.html（分组视图「新建分组」钮原文）
  'folder-plus': (
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
      <path d="M12 10v6M9 13h6" />
    </>
  ),
  // archive ← app-shell.html（两视图工具条「归档」钮原文）
  archive: (
    <>
      <path d="M3 4h18v4H3V4z" />
      <path d="M5 8v11h14V8M9 12h6" />
    </>
  ),
  // filter ← app-shell.html（项目视图「筛选和排序」钮原文）
  filter: <path d="M4 5h16l-6 7v5l-4 2v-7L4 5z" />,
  // move ← app-shell.html（项目分区「移动项目分区」钮原文，四向箭头）
  move: (
    <path d="M5 9l-3 3 3 3M9 5l3-3 3 3M15 19l-3 3-3-3M19 9l3 3-3 3M2 12h20M12 2v20" />
  ),
  // move-h ← app-shell.html（任务分区「移动任务分区」钮原文，横向变体）
  'move-h': <path d="M5 9l-3 3 3 3M19 9l3 3-3 3M2 12h20" />,
  // gift ← app-shell.html（quota-tip 成本提醒胶囊原文）
  gift: (
    <>
      <rect x="3" y="8" width="18" height="4" rx="1" />
      <path d="M5 12v9h14v-9M12 8v13" />
      <path d="M12 8c-1.5 0-4.5-.5-4.5-2.5S10 3 12 8c2-5 4.5-4.5 4.5-2.5S13.5 8 12 8z" />
    </>
  ),
  // file-text ← app-shell.html（快捷卡「周报总结」原文）
  'file-text': (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z" />
      <path d="M14 3v5h5" />
    </>
  ),
  // pulse ← app-shell.html（快捷卡「报错修复」原文）
  pulse: <path d="M22 12h-4l-3 8L9 4l-3 8H2" />,
  // present ← app-shell.html（快捷卡「PPT 制作」原文）
  present: <path d="M3 4h18M5 4v10h14V4M12 14v3M8 21l4-4 4 4" />,
  // globe ← app-shell.html（面板选择器「浏览器」钮原文）
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z" />
    </>
  ),
  // review-check ← app-shell.html（面板选择器「审查」钮原文，勾选框形）
  'review-check': (
    <>
      <path d="M9 11l3 3 8-8" />
      <path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9" />
    </>
  ),
  // settings ← app-shell.html（侧栏「设置」钮原文，与 sun 同 path，语义独立命名）
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" />
    </>
  ),
  // ---- B2 批新增（task-conversation.html 内联 SVG 原文）----
  // check ← task-conversation.html（todo mk/计时器完成/迭代横幅勾原文）
  check: <path d="M5 13l4 4L19 7" />,
  // thumbs-up ← task-conversation.html（完成态操作栏「赞」原文）
  'thumbs-up': (
    <path d="M7 11v9M7 11H4v9h3M7 11l4-7c1.5 0 2.6 1 2.6 2.5L13 11h5a2 2 0 0 1 2 2.2L18.6 19a2 2 0 0 1-2 1H7" />
  ),
  // thumbs-down ← 「踩」= 赞形 rotate(180)（原型 transform 同款）
  'thumbs-down': (
    <g transform="rotate(180 12 12)">
      <path d="M7 11v9M7 11H4v9h3M7 11l4-7c1.5 0 2.6 1 2.6 2.5L13 11h5a2 2 0 0 1 2 2.2L18.6 19a2 2 0 0 1-2 1H7" />
    </g>
  ),
  // fork ← task-conversation.html（完成态操作栏「分叉」原文）
  fork: (
    <path d="M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 15a9 9 0 0 1 9-9" />
  ),
  // hook ← task-conversation.html（完成态操作栏「钩子」原文，P1 占位钮）
  hook: (
    <>
      <circle cx="12" cy="4.5" r="1.6" />
      <path d="M12 6.1V15a5 5 0 0 1-9.2 2.7" />
      <path d="M2.8 17.7L4 14.9l2.6 1.6" />
    </>
  ),
  // share ← task-conversation.html（任务头部「分享」原文）
  share: <path d="M4 12v8h16v-8M12 2v13M8 6l4-4 4 4" />,
  // sliders ← task-conversation.html（状态面板头部滑杆形原文）
  sliders: (
    <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" />
  ),
  // user ← task-conversation.html（子智能体卡 .tic 人形原文）
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c0-4 4-6 8-6s8 2 8 6" />
    </>
  ),
  // wrap ← task-conversation.html（代码块「自动换行」钮原文）
  wrap: <path d="M4 7h16M4 12h10M4 17h16" />,
  // ---- B3 批新增（approval-card.html / changed-files.html 内联 SVG 原文）----
  // triangle-alert ← approval-card.html（.need-p「需要权限」警示三角原文）
  'triangle-alert': (
    <path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
  ),
  // undo ← changed-files.html（改动文件卡「撤销」钮原文）
  undo: <path d="M3 3v5h5M3 8a9 9 0 1 1-2.2 5.8" />,
  // file-plus ← changed-files.html（.file-head Git 状态文件图标原文：file-text + 加号）
  'file-plus': (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z" />
      <path d="M14 3v5h5M12 11v6M9 14h6" />
    </>
  ),
  // ---- B4 批新增（side-panels.html 内联 SVG 原文）----
  // message-square ← side-panels.html（面板选择器「辅助对话」原文）
  'message-square': <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
  // code ← side-panels.html（面板选择器「源代码查看」原文）
  code: <path d="M16 18l6-6-6-6M8 6l-6 6 6 6" />,
  // ---- B5 批新增（toolbar-menus.html 内联 SVG 原文）----
  // checkbox ← toolbar-menus.html（权限模式「计划模式」checkbox 框原文）
  checkbox: <rect x="4" y="4" width="16" height="16" rx="3" />,
  // radio ← toolbar-menus.html（权限模式单选圈未选态原文：外圈 + 内圈描边）
  radio: (
    <>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3.5" />
    </>
  ),
  // radio-on ← toolbar-menus.html（单选圈选中态原文：内圆填充）
  'radio-on': (
    <>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none" />
    </>
  ),
  // git-graph ← toolbar-menus.html（分支切换 dialog「Git 图谱」钮原文）
  'git-graph': (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="12" r="2.5" />
      <path d="M6 8.5v7M8.5 6.8C13 8 15 9.5 15.6 10.4" />
    </>
  ),
  // ---- B6 批新增（command-palette.html / task-menus.html / empty-states.html 内联 SVG 原文）----
  // star ← task-menus.html（「更多」菜单「置顶任务」原文）
  star: (
    <path d="M12 2l3 6 6 1-4.5 4.3L18 20l-6-3.2L6 20l1.5-6.7L3 9l6-1z" />
  ),
  // bell ← task-menus.html（「标记为未读」原文）
  bell: (
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M10.3 21a2 2 0 0 0 3.4 0" />
  ),
  // link ← task-menus.html（「复制路径」族链形原文）
  link: (
    <>
      <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5" />
      <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7L12.5 19.5" />
    </>
  ),
  // chart ← command-palette.html（审查标签条目）/ task-menus.html（「查看调用轨迹」原文）
  chart: (
    <>
      <path d="M3 3v18h18" />
      <path d="M7 14l4-4 3 3 5-6" />
    </>
  ),
  // list ← empty-states.html（任务区空态图标原文：三行 + 行首圆点）
  list: (
    <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />
  ),
  // search-minus ← empty-states.html（命令面板空搜索图标原文：搜索镜 + 减号线）
  'search-minus': (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3M8 11h6" />
    </>
  ),
  // sparkle ← command-palette.html（「技能」原文）
  sparkle: (
    <path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4L12 3z" />
  ),
  // panel-left ← command-palette.html（「切换侧边栏」原文，rect + 左缘竖线）
  'panel-left': (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
    </>
  ),
  // ---- B7 批新增（login.html 内联 SVG 原文）----
  // key ← login.html（三入口「使用 API key」钥匙形原文）
  key: (
    <>
      <circle cx="8" cy="14" r="4" />
      <path d="M11 11L20 2M16 6l3 3M13 9l2 2" />
    </>
  ),
};

export interface IconProps {
  name: IconName;
  /** 渲染边长（px），默认 14 = 原型 `.ic` 尺寸 */
  size?: number;
  className?: string;
}

export function Icon({ name, size = 14, className }: IconProps): ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
      style={{ width: size, height: size, flex: 'none' }}
    >
      {GLYPHS[name]}
    </svg>
  );
}
