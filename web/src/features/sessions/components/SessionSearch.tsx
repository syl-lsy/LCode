import { useId, useState, type Ref } from 'react';

import { Icon } from '@/components/Icon';

/**
 * 会话搜索框（AC-004-3）：标题子串过滤，输入直写 sessionStore.query。
 * 纯受控组件：值与回调由 Sidebar 注入（store 读取集中在 Sidebar 一处）；
 * inputRef 供侧栏「搜索」钮聚焦输入框（原型 app-shell 搜索钮的落地形态）。
 */
export interface SessionSearchProps {
  value: string;
  onChange: (query: string) => void;
  /** 透传到 input（React 19 ref 即普通 prop） */
  inputRef?: Ref<HTMLInputElement>;
}

export function SessionSearch({ value, onChange, inputRef }: SessionSearchProps) {
  const inputId = useId();
  const [focused, setFocused] = useState(false);
  return (
    <div className="relative px-2.5 pb-1.5">
      <Icon
        name="search"
        size={13}
        className="pointer-events-none absolute top-1/2 left-5 -translate-y-1/2 text-foreground-subtlest"
      />
      <label className="sr-only" htmlFor={inputId}>
        搜索会话
      </label>
      <input
        id={inputId}
        ref={inputRef}
        data-testid="session-search"
        type="search"
        value={value}
        placeholder="搜索会话"
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        className={`h-7 w-full rounded-md border bg-input px-7 text-[length:var(--text-ui-xs)] text-foreground outline-none placeholder:text-foreground-subtlest ${
          focused ? 'border-accent-border' : 'border-border'
        }`}
      />
    </div>
  );
}
