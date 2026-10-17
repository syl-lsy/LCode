/**
 * 右栏多用面板空壳（A4 §2.1 features/workbench，RD#45 通用容器）。
 * M2 任务 2/6 接入：面板壳（收起 + 220-480px 拖拽）/PanelTabs/审查与终端子面板 +
 * workbenchStore（tabs 开放集合；唯一允许跨 feature 调 action 的 UI 编排 store）。
 */
export function WorkbenchPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 p-4">
      <span className="font-mono text-[length:var(--text-ui-xs)] text-foreground-subtle">
        workbench · 多用面板
      </span>
      <span className="font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        M2 任务 2/6 接入
      </span>
    </div>
  );
}
