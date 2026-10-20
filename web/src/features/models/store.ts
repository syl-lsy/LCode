import { create } from 'zustand';

import type { ModelInfo, ModelList, SessionDetail } from '@contracts/rest';

/**
 * modelsStore：模型列表 + 两旋钮选择（A4 §3.2 modelsStore 行；REQ-014 / AC-014-1）。
 *
 * 写入者：REST（GET /models、PATCH /sessions/{id} 回声）与用户动作（选择器）。
 * UI 只暴露模型选择器 + reasoning-effort 档位（AGENTS.md 硬约束：不暴露采样参数）。
 */
export type ReasoningEffort = SessionDetail['reasoning_effort'];

export interface ModelSelection {
  readonly model: string;
  readonly effort: ReasoningEffort;
}

export interface ModelsStore {
  models: ModelInfo[];
  defaultModel: string | null;
  selected: ModelSelection | null;
  setFromModelList: (list: ModelList) => void;
  select: (selection: ModelSelection) => void;
  /** PATCH 回声/会话详情同步（AC-014-2 热切换以服务端回声为准） */
  syncFromSession: (session: SessionDetail) => void;
  reset: () => void;
}

interface ModelsData {
  models: ModelInfo[];
  defaultModel: string | null;
  selected: ModelSelection | null;
}

function createModelsSlice(): ModelsData {
  return { models: [], defaultModel: null, selected: null };
}

/** 默认选中：模型默认项（is_default）→ 列表首项；档位取该模型能力集里最接近默认的档。 */
function initialSelection(list: ModelList): ModelSelection | null {
  const model = list.models.find((m) => m.id === list.default_model) ?? list.models[0];
  if (!model) return null;
  const effort = model.reasoning_efforts.includes('medium')
    ? 'medium'
    : (model.reasoning_efforts[0] ?? 'off');
  return { model: model.id, effort };
}

export const useModelsStore = create<ModelsStore>((set) => ({
  ...createModelsSlice(),
  setFromModelList: (list) =>
    set((s) => ({
      models: [...list.models],
      defaultModel: list.default_model,
      selected: s.selected ?? initialSelection(list),
    })),
  select: (selection) => set({ selected: selection }),
  syncFromSession: (session) =>
    set((s) => ({
      selected: {
        model: session.model,
        effort: session.reasoning_effort,
      },
      // 会话回声里的模型不在列表（配置变了/列表未加载）不补齐 models，避免伪造能力面
      defaultModel: s.defaultModel,
    })),
  reset: () => set(createModelsSlice()),
}));
