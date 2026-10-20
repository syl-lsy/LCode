import { useEffect } from 'react';

import { listModels } from '@/api/models';
import { patchSession } from '@/api/sessions';
import { ApiError } from '@/api/http';

import { useModelsStore } from '../store';

import { Menu } from '@/components/Menu';

/**
 * 模型选择器（A4 §8.1 ModelSelector 行；F8 单层菜单；AC-014-1/2）。
 *
 * 两旋钮之一——UI 只暴露模型选择 + reasoning-effort 档位，不暴露采样参数
 * （AGENTS.md 硬约束 / REQ-014）。热切换：选中即 PATCH /sessions/{id}
 * （下条消息生效，AC-014-2），以服务端回声 syncFromSession 为准；sessionId 为空
 * （首页）只更新本地选择，待建会话时随 SessionCreate 携带。
 */
export interface ModelSelectorProps {
  /** 当前会话 id；空 = 未建会话（仅本地选择） */
  sessionId?: string | null;
}

export function ModelSelector({ sessionId }: ModelSelectorProps) {
  const models = useModelsStore((s) => s.models);
  const selected = useModelsStore((s) => s.selected);
  const setFromModelList = useModelsStore((s) => s.setFromModelList);
  const select = useModelsStore((s) => s.select);
  const syncFromSession = useModelsStore((s) => s.syncFromSession);

  const loaded = models.length > 0;
  useEffect(() => {
    if (loaded) return;
    let cancelled = false;
    listModels()
      .then((list) => {
        if (!cancelled) setFromModelList(list);
      })
      .catch((error: unknown) => {
        console.error('[models] 模型列表加载失败：', error);
      });
    return () => {
      cancelled = true;
    };
  }, [loaded, setFromModelList]);

  if (!loaded || selected === null) {
    return (
      <button
        type="button"
        data-testid="model-selector"
        disabled
        className="tool-btn font-mono"
        aria-label="模型选择器（加载中）"
      >
        模型…
      </button>
    );
  }

  const current = models.find((m) => m.id === selected.model);

  const selectModel = (modelId: string): void => {
    const model = models.find((m) => m.id === modelId);
    if (model === undefined) return;
    // 档位随模型走：新模型能力集不含当前档位时回退到其中一档（AC-014-3 联动）
    const effort = model.reasoning_efforts.includes(selected.effort)
      ? selected.effort
      : (model.reasoning_efforts.at(-1) ?? 'off');
    select({ model: modelId, effort });
    if (sessionId == null) return;
    patchSession(sessionId, { model: modelId, reasoning_effort: effort })
      .then((echo) => syncFromSession(echo))
      .catch((error: unknown) => {
        if (error instanceof ApiError) {
          console.error(`[models] 热切换失败（${error.code}）：`, error.message);
        } else {
          console.error('[models] 热切换失败：', error);
        }
      });
  };

  return (
    <Menu
      trigger={
        <>
          <span className="font-mono">{current?.display_name ?? selected.model}</span>
          <span aria-hidden="true" className="text-foreground-subtlest">
            ▾
          </span>
        </>
      }
      triggerClassName="tool-btn"
      triggerLabel="模型选择器"
      triggerTestId="model-selector"
      menuLabel="选择模型"
      align="left"
      items={models.map((model) => ({
        key: model.id,
        label: <span className="font-mono">{model.display_name}</span>,
        selected: model.id === selected.model,
        onSelect: () => selectModel(model.id),
      }))}
    />
  );
}
