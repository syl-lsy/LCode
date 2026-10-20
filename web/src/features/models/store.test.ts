/**
 * modelsStore 单测（A4 §3.2 modelsStore 行；REQ-014 / AC-014-1/2/3）。
 *
 * 重点：默认选中推导（is_default → 列表首项；档位取 medium 否则能力集首档）；
 * 用户选择覆盖默认；PATCH 回声同步（不补齐不存在的模型）；档位集合来自契约
 * reasoning_efforts（置灰判据）。
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { useModelsStore } from './store';
import type { ModelList } from '@contracts/rest';

const LIST: ModelList = {
  default_model: 'qwen3-coder-plus',
  models: [
    {
      id: 'qwen3-coder-plus',
      display_name: 'Qwen3 Coder Plus',
      provider: 'dashscope',
      reasoning_efforts: ['off', 'low', 'medium', 'high'],
      supports_reasoning: true,
      is_default: true,
    },
    {
      id: 'qwen3-max',
      display_name: 'Qwen3 Max',
      provider: 'dashscope',
      reasoning_efforts: ['off', 'medium'],
      supports_reasoning: true,
    },
    {
      id: 'qwen-turbo',
      display_name: 'Qwen Turbo',
      provider: 'dashscope',
      reasoning_efforts: [],
      supports_reasoning: false,
    },
  ],
};

beforeEach(() => {
  useModelsStore.getState().reset();
});

describe('modelsStore', () => {
  it('setFromModelList：列表 + default_model + 默认选中（默认项 + medium 档）', () => {
    useModelsStore.getState().setFromModelList(LIST);

    const state = useModelsStore.getState();
    expect(state.models).toHaveLength(3);
    expect(state.defaultModel).toBe('qwen3-coder-plus');
    expect(state.selected).toEqual({ model: 'qwen3-coder-plus', effort: 'medium' });
  });

  it('无 is_default 时取列表首项；能力集无 medium 时取首档', () => {
    useModelsStore.getState().setFromModelList({
      default_model: 'nonexistent',
      models: LIST.models,
    });
    expect(useModelsStore.getState().selected).toEqual({ model: 'qwen3-coder-plus', effort: 'medium' });

    useModelsStore.getState().reset();
    useModelsStore.getState().setFromModelList({
      default_model: 'qwen-turbo',
      models: [LIST.models[2] as ModelList['models'][number]],
    });
    // 空能力集：档位回退 'off'（UI 层两旋钮为空的置灰依据）
    expect(useModelsStore.getState().selected).toEqual({ model: 'qwen-turbo', effort: 'off' });
  });

  it('select：用户选择覆盖默认（热切换后的本地即时态）', () => {
    useModelsStore.getState().setFromModelList(LIST);
    useModelsStore.getState().select({ model: 'qwen3-max', effort: 'high' });

    expect(useModelsStore.getState().selected).toEqual({ model: 'qwen3-max', effort: 'high' });
  });

  it('select 高于模型能力集的档位也如实记录（置灰过滤归 UI，store 不裁决）', () => {
    useModelsStore.getState().setFromModelList(LIST);
    // qwen-turbo 的 reasoning_efforts 为空——是否可选由 EffortSelector 依契约数据置灰
    useModelsStore.getState().select({ model: 'qwen-turbo', effort: 'off' });
    expect(useModelsStore.getState().selected).toEqual({ model: 'qwen-turbo', effort: 'off' });
  });

  it('syncFromSession：PATCH 回声同步选中；未加载列表不补 models（防伪造能力面）', () => {
    useModelsStore.getState().syncFromSession({
      id: 's1',
      title: 't',
      status: 'active',
      model: 'qwen3-max',
      reasoning_effort: 'low',
      approval_mode: 'manual',
      sandbox_provider: 'opensandbox',
      run_state: 'idle',
      created_at: 't',
      updated_at: 't',
      last_active_at: 't',
    });

    expect(useModelsStore.getState().selected).toEqual({ model: 'qwen3-max', effort: 'low' });
    expect(useModelsStore.getState().models).toEqual([]);
  });
});
