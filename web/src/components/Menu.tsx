import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { Icon } from './Icon';

/**
 * 浮层菜单基件（toolbar-menus 八菜单共用，A4 §2.1 components/Menu.tsx）。
 *
 * 触发器 + 弹出菜单：外部点击 / Escape 关闭；aria-haspopup/aria-expanded/
 * role="menu|menuitem" 与 aria-checked 支撑读屏。纯展示：状态自持、动作回调出去。
 *
 * B5 升级（对照 design/prototype/toolbar-menus.html 一比一）：
 * - 容器与菜单项改用原型 .menu / .mi 族类（components.css，B5 落位），
 *   弹出方向支持 up（输入区 / 侧栏底部菜单）与 down（默认）；
 * - MenuItem 增加 kind（separator/header/static/foot）、leading / desc /
 *   trailing / checkFirst / itemClassName / title 槽位，承载原型的
 *   组头、两行命令卡、子菜单箭头、前置单选/复选图标与选中勾前后置等结构。
 */

export interface MenuItem {
  readonly key: string;
  /**
   * 项形态：item（默认，交互按钮 .mi）/ separator 分隔线 / header 组头 /
   * static 静态行（不可交互，配 itemClassName）/ foot 底部提示区。
   */
  readonly kind?: 'item' | 'separator' | 'header' | 'static' | 'foot';
  /**
   * 内容：item = 名称区（可含 .nm / .cmd-line 等原型结构）；
   * header / static / foot = 整块内容。
   */
  readonly label?: ReactNode;
  /** item 名称后的行内说明（.ds，原型菜单 2/4 档说明文案） */
  readonly desc?: ReactNode;
  /** item 前置图标（.ic 口径，如单选圈 / checkbox / 管理模型齿轮） */
  readonly leading?: ReactNode;
  /** item 右侧自定义（.arw 子菜单箭头 / 徽标；与选中勾共存时勾在其后） */
  readonly trailing?: ReactNode;
  /** 选中勾放最前（原型模型选择器 / 添加上下文 / 分支切换口径：.ck 为首子节点）；默认勾在最右 */
  readonly checkFirst?: boolean;
  readonly disabled?: boolean;
  /** 选中态标记（aria-checked；选择器类菜单用） */
  readonly selected?: boolean;
  /** 覆盖 item 默认类（如账号菜单头 .acct、子菜单父行 .mi.sub-parent） */
  readonly itemClassName?: string;
  /** 原生 title（占位说明等） */
  readonly title?: string;
  /** 选中后保持菜单展开（如账号菜单 ID 复制的就地反馈，原型 F16 脚本口径） */
  readonly keepOpen?: boolean;
  readonly onSelect?: () => void;
}

export interface MenuProps {
  /** 触发按钮内容 */
  trigger: ReactNode;
  items: readonly MenuItem[];
  /** 触发按钮额外类（tool-btn 风格由调用方给） */
  triggerClassName?: string;
  /** 触发按钮 aria-label（触发内容不是纯文字时必给） */
  triggerLabel?: string;
  /** 触发按钮 data-testid（选择器类组件的稳定定位钩子） */
  triggerTestId?: string;
  /** 菜单弹出方向（默认向下；输入区 / 侧栏底部菜单用 up） */
  direction?: 'up' | 'down';
  /** 菜单弹出水平对齐（默认 left；靠右触发钮用 right） */
  align?: 'left' | 'right';
  /** 覆盖 .menu 默认 400px 最大宽（原型各菜单的 inline max-width） */
  menuMaxWidth?: number;
  /** 菜单 aria-label */
  menuLabel?: string;
  /** 根容器附加类（如侧栏账号钮需要 w-full 撑满） */
  rootClassName?: string;
  disabled?: boolean;
}

export function Menu({
  trigger,
  items,
  triggerClassName,
  triggerLabel,
  triggerTestId,
  direction = 'down',
  align = 'left',
  menuMaxWidth,
  menuLabel,
  rootClassName,
  disabled = false,
}: MenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent): void => {
      if (rootRef.current !== null && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`relative inline-flex ${rootClassName ?? ''}`}>
      <button
        type="button"
        className={triggerClassName}
        data-testid={triggerTestId}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={triggerLabel}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
      >
        {trigger}
      </button>
      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label={menuLabel}
          data-testid="menu-popover"
          className={`menu absolute z-50 min-w-[180px] ${
            direction === 'up' ? 'bottom-[calc(100%+4px)]' : 'top-[calc(100%+4px)]'
          } ${align === 'right' ? 'right-0' : 'left-0'}`}
          style={menuMaxWidth === undefined ? undefined : { maxWidth: menuMaxWidth }}
        >
          {items.map((item) => renderMenuItem(item, () => setOpen(false)))}
        </div>
      )}
    </div>
  );
}

function renderMenuItem(item: MenuItem, close: () => void): ReactNode {
  const testId = `menu-item-${item.key}`;
  if (item.kind === 'separator') {
    return <div key={item.key} role="separator" data-testid={testId} className="sep" />;
  }
  if (item.kind === 'header') {
    return (
      <div key={item.key} data-testid={testId} className="hdr">
        {item.label}
      </div>
    );
  }
  if (item.kind === 'foot') {
    return (
      <div key={item.key} data-testid={testId} className="menu-foot">
        {item.label}
      </div>
    );
  }
  if (item.kind === 'static') {
    return (
      <div key={item.key} data-testid={testId} className={item.itemClassName ?? 'searching'}>
        {item.label}
      </div>
    );
  }
  // item（默认）：交互按钮；带选中态的项用 menuitemradio（aria-checked 在 menuitem
  // 上不合法，axe 报 aria-allowed-attr）；普通动作项保持 menuitem。
  const itemRole = item.selected === undefined ? 'menuitem' : 'menuitemradio';
  const check =
    item.selected === true ? <Icon name="check" size={12} className="ck" /> : null;
  return (
    <button
      key={item.key}
      type="button"
      role={itemRole}
      aria-checked={item.selected}
      disabled={item.disabled}
      title={item.title}
      data-testid={testId}
      className={
        item.itemClassName ??
        `mi${item.selected === true ? ' sel' : ''}${item.disabled === true ? ' disabled' : ''}`
      }
      onClick={() => {
        if (item.disabled) return;
        item.onSelect?.();
        if (item.keepOpen !== true) close();
      }}
    >
      {item.leading}
      {item.checkFirst === true && check}
      {item.label}
      {item.desc}
      {item.trailing}
      {item.checkFirst !== true && check}
    </button>
  );
}
