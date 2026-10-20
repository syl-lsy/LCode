import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react';

/**
 * 浮层菜单基件（toolbar-menus 八菜单共用，A4 §2.1 components/Menu.tsx）。
 *
 * 触发器 + 弹出菜单：外部点击 / Escape 关闭；aria-haspopup/aria-expanded/
 * role="menu|menuitem" 与 aria-checked 支撑读屏。纯展示：状态自持、动作回调出去。
 */

export interface MenuItem {
  readonly key: string;
  readonly label: ReactNode;
  readonly disabled?: boolean;
  /** 选中态标记（aria-checked；选择器类菜单用） */
  readonly selected?: boolean;
  /** 右侧说明/快捷键等次要文字 */
  readonly hint?: ReactNode;
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
  /** 菜单弹出方向（默认向下） */
  align?: 'left' | 'right';
  /** 菜单 aria-label */
  menuLabel?: string;
  disabled?: boolean;
}

export function Menu({
  trigger,
  items,
  triggerClassName,
  triggerLabel,
  triggerTestId,
  align = 'left',
  menuLabel,
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
    <div ref={rootRef} className="relative inline-flex">
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
          className={`absolute top-[calc(100%+4px)] z-50 min-w-[180px] rounded-md border border-border-hover bg-popover p-1 shadow-lg ${
            align === 'right' ? 'right-0' : 'left-0'
          }`}
        >
          {items.map((item) => {
            // 有选中态的项用 menuitemradio（aria-checked 在 menuitem 上不合法，axe 报
            // aria-allowed-attr）；普通动作项保持 menuitem。
            const itemRole = item.selected === undefined ? 'menuitem' : 'menuitemradio';
            return (
              <button
                key={item.key}
                type="button"
                role={itemRole}
                aria-checked={item.selected}
                disabled={item.disabled}
                data-testid={`menu-item-${item.key}`}
                className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[length:var(--text-ui-xs)] text-foreground hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-transparent"
                onClick={() => {
                  if (item.disabled) return;
                  item.onSelect?.();
                  setOpen(false);
                }}
              >
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                {item.hint !== undefined && (
                  <span className="flex-none text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
                    {item.hint}
                  </span>
                )}
                {item.selected === true && (
                  <span aria-hidden="true" className="flex-none text-brand">
                    ✓
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
