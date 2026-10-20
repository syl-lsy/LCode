import { useId, useState } from 'react';

/**
 * 会话搜索框（AC-004-3）：标题子串过滤，输入直写 sessionStore.query。
 * 纯受控组件：值与回调由 Sidebar 注入（store 读取集中在 Sidebar 一处）。
 */
export interface SessionSearchProps {
  value: string;
  onChange: (query: string) => void;
}

export function SessionSearch({ value, onChange }: SessionSearchProps) {
  const inputId = useId();
  const [focused, setFocused] = useState(false);
  return (
    <div className="relative px-2.5 pb-1.5">
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-5 -translate-y-1/2 text-[length:var(--text-ui-xs)] text-foreground-subtlest"
      >
        ⌕
      </span>
      <label className="sr-only" htmlFor={inputId}>
        搜索会话
      </label>
      <input
        id={inputId}
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
