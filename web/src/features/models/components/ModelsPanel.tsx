/**
 * 模型选择与档位空壳（A4 §2.1 features/models，REQ-014）。
 * M2 任务 7 接入：ModelSelector（单层菜单）/EffortSelector（档位随模型过滤置灰）
 * + modelsStore + api/models.ts（AC-014-1/2/3）。
 * 挂载点在工具条与 Composer 两旋钮区——UI 只暴露模型选择器 + reasoning-effort
 * 档位，不暴露采样参数（AGENTS.md 硬约束）。
 */
export function ModelsPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 p-4">
      <span className="font-mono text-[length:var(--text-ui-xs)] text-foreground-subtle">
        models · 模型与档位
      </span>
      <span className="font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
        M2 任务 7 接入
      </span>
    </div>
  );
}
