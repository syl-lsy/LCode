import { patchSession } from '@/api/sessions';
import { ApiError } from '@/api/http';

import { useModelsStore, type ReasoningEffort } from '../store';

import { Menu } from '@/components/Menu';

/**
 * 档位选择器（A4 §8.1 EffortSelector 行；F7；AC-014-3）。
 *
 * reasoning-effort 档位随当前模型过滤：模型能力集（ModelInfo.reasoning_efforts，
 * 服务端按 param_support 过滤）之外的档位置灰不可选。切换即 PATCH（sessionId 非空），
 * 以回声为准。
 */

const EFFORT_LABEL: Record<ReasoningEffort, string> = {
  off: '关闭',
  low: '低',
  medium: '中',
  high: '最高',
};

const EFFORT_ORDER: readonly ReasoningEffort[] = ['off', 'low', 'medium', 'high'];

export interface EffortSelectorProps {
  sessionId?: string | null;
}

export function EffortSelector({ sessionId }: EffortSelectorProps) {
  const models = useModelsStore((s) => s.models);
  const selected = useModelsStore((s) => s.selected);
  const select = useModelsStore((s) => s.select);
  const syncFromSession = useModelsStore((s) => s.syncFromSession);

  if (selected === null) {
    return (
      <button
        type="button"
        data-testid="effort-selector"
        disabled
        className="tool-btn"
        aria-label="推理档位（加载中）"
      >
        档位…
      </button>
    );
  }

  const model = models.find((m) => m.id === selected.model);
  // 能力集未知（列表未含当前模型，如会话回声先到）：不置灰，全档可见
  const supported: ReadonlySet<ReasoningEffort> =
    model === undefined ? new Set(EFFORT_ORDER) : new Set(model.reasoning_efforts);

  const selectEffort = (effort: ReasoningEffort): void => {
    select({ model: selected.model, effort });
    if (sessionId == null) return;
    patchSession(sessionId, { reasoning_effort: effort })
      .then((echo) => syncFromSession(echo))
      .catch((error: unknown) => {
        if (error instanceof ApiError) {
          console.error(`[models] 档位切换失败（${error.code}）：`, error.message);
        } else {
          console.error('[models] 档位切换失败：', error);
        }
      });
  };

  return (
    <Menu
      trigger={
        <>
          <span>{EFFORT_LABEL[selected.effort]}</span>
          <span aria-hidden="true" className="text-foreground-subtlest">
            ▾
          </span>
        </>
      }
      triggerClassName="tool-btn"
      triggerLabel="推理档位"
      triggerTestId="effort-selector"
      menuLabel="选择推理档位"
      align="left"
      items={EFFORT_ORDER.map((effort) => ({
        key: effort,
        label: EFFORT_LABEL[effort],
        hint: <span className="font-mono">{effort}</span>,
        disabled: !supported.has(effort),
        selected: effort === selected.effort,
        onSelect: () => selectEffort(effort),
      }))}
    />
  );
}
