/**
 * 左栏会话列表空壳（A4 §2.1 features/sessions）。
 * M2 任务 3 接入：Sidebar/SessionItem/SessionSearch + sessionStore + api/sessions.ts
 * （AC-004-1/3：列表排序/搜索/新建/切换/删除、刷新恢复）。
 */
export function SessionsPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 p-4">
      <span className="font-mono text-[length:var(--text-ui-xs)] text-foreground-subtle">
        sessions · 会话列表
      </span>
      <span className="font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        M2 任务 3 接入
      </span>
    </div>
  );
}
