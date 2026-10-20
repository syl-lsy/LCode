// @vitest-environment jsdom
/**
 * 模型两旋钮（test-plan §2.5；A4 §8.1 ModelSelector/EffortSelector 行）：
 * - AC-014-1：只有「模型 + 档位」两个旋钮，界面不得出现任何采样参数（temperature/top_p/top_k）；
 * - AC-014-2：选择即 PATCH /sessions/{id}（下条消息生效），以回声 syncFromSession 为准；
 * - AC-014-3：档位随模型能力集过滤——不在 reasoning_efforts 内的档位置灰。
 * 文件名沿用测试计划（ModelPicker.test.tsx = 两旋钮组件）。
 */
import { render, screen, waitFor, within, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { http, HttpResponse } from 'msw';

import { modelListFixture, requestLog } from '../../../tests/msw-handlers';
import { server } from '../../../tests/test-utils';
import { useModelsStore } from '../store';

import { EffortSelector } from './EffortSelector';
import { ModelSelector } from './ModelSelector';

const SESSION = 'sess-a';

/** EffortSelector 不负责拉模型列表（ModelSelector 才拉）：单独测它时先种子化 store。 */
function seedModels(): void {
  useModelsStore.getState().setFromModelList(modelListFixture);
  useModelsStore.getState().select({ model: 'mock/qwen/qwen3-flash', effort: 'medium' });
}

async function waitForModelSelection(): Promise<void> {
  await waitFor(() => {
    expect(useModelsStore.getState().selected).not.toBeNull();
  });
}

describe('ModelPicker · 两旋钮与档位置灰（AC-014-1/3）', () => {
  it('test_only_model_and_effort_knobs：界面仅模型与档位；无任何采样参数暴露', async () => {
    render(
      <>
        <ModelSelector sessionId={SESSION} />
        <EffortSelector sessionId={SESSION} />
      </>,
    );
    await waitForModelSelection();

    const text = document.body.textContent ?? '';
    for (const forbidden of ['temperature', 'top_p', 'top_k', 'topP', '采样', '温度']) {
      expect(text.toLowerCase()).not.toContain(forbidden.toLowerCase());
    }
    // 两个旋钮都在（模型下拉 + 档位下拉）
    expect(screen.getByTestId('model-selector')).toBeInTheDocument();
    expect(screen.getByTestId('effort-selector')).toBeInTheDocument();
  });

  it('模型列表来自 GET /models；默认选中 default_model（当前位置文本可见）', async () => {
    render(<ModelSelector sessionId={SESSION} />);
    await waitFor(() => {
      expect(screen.getByTestId('model-selector')).toHaveTextContent('qwen3-flash');
    });
    expect(useModelsStore.getState().defaultModel).toBe('mock/qwen/qwen3-flash');
  });

  it('单层菜单列出全部模型并标记当前项', async () => {
    const user = userEvent.setup();
    render(<ModelSelector sessionId={SESSION} />);
    await waitFor(() => expect(screen.getByTestId('model-selector')).toHaveTextContent('qwen3-flash'));

    await user.click(screen.getByTestId('model-selector'));
    const menu = screen.getByTestId('menu-popover');
    // 模型菜单项都带选中态标记（menuitemradio）
    expect(within(menu).getAllByRole('menuitemradio')).toHaveLength(2);
    expect(within(menu).getByTestId('menu-item-mock/qwen/qwen3-flash')).toHaveAttribute('aria-checked', 'true');
  });

  it('test_patch_then_next_message：选择模型 → PATCH /sessions/{id} 且以回声更新选择（AC-014-2）', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    render(<ModelSelector sessionId={SESSION} />);
    await waitFor(() => expect(screen.getByTestId('model-selector')).toHaveTextContent('qwen3-flash'));

    await user.click(screen.getByTestId('model-selector'));
    await user.click(screen.getByTestId('menu-item-mock/qwen/qwen3-coder-plus'));

    await waitFor(() => {
      const patch = requestLog.find((entry) => entry.method === 'PATCH');
      expect(patch?.url).toContain('/sessions/sess-a');
      expect(patch?.body).toMatchObject({ model: 'mock/qwen/qwen3-coder-plus' });
    });
    await waitFor(() => {
      expect(useModelsStore.getState().selected?.model).toBe('mock/qwen/qwen3-coder-plus');
    });
    // 档位随模型收敛到能力集内（coder-plus 仅 high）
    expect(useModelsStore.getState().selected?.effort).toBe('high');
  });

  it('无会话（首页）：选择只改本地 state，不发 PATCH（会话创建时随 SessionCreate 携带）', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    render(<ModelSelector sessionId={null} />);
    await waitFor(() => expect(screen.getByTestId('model-selector')).toHaveTextContent('qwen3-flash'));
    await user.click(screen.getByTestId('model-selector'));
    await user.click(screen.getByTestId('menu-item-mock/qwen/qwen3-coder-plus'));
    expect(useModelsStore.getState().selected?.model).toBe('mock/qwen/qwen3-coder-plus');
    expect(requestLog.filter((entry) => entry.method === 'PATCH')).toHaveLength(0);
  });

  it('PATCH 失败：保留旧选择并 console.error（不假装成功）', async () => {
    const user = userEvent.setup();
    server.use(
      http.patch('*/api/v1/sessions/:id', () =>
        HttpResponse.json({ code: 'PARAM_INVALID', message: '模型不存在' }, { status: 422 }),
      ),
    );
    render(<ModelSelector sessionId={SESSION} />);
    await waitFor(() => expect(screen.getByTestId('model-selector')).toHaveTextContent('qwen3-flash'));
    await user.click(screen.getByTestId('model-selector'));
    await user.click(screen.getByTestId('menu-item-mock/qwen/qwen3-coder-plus'));
    await waitFor(() => {
      expect(useModelsStore.getState().selected?.model).toBe('mock/qwen/qwen3-coder-plus');
    });
    // 服务端回声未到 → 无 syncFromSession；本地选择保留（下条消息仍会带新模型）
    expect(useModelsStore.getState().selected?.model).toBe('mock/qwen/qwen3-coder-plus');
  });
});

describe('EffortSelector · 档位随模型过滤置灰（AC-014-3）', () => {
  it('test_unsupported_option_disabled：能力集外的档位置灰不可选', async () => {
    const user = userEvent.setup();
    seedModels();
    render(<EffortSelector sessionId={SESSION} />);
    // 默认模型 flash：能力集 [off, low, medium, high] → 全档可用
    await user.click(screen.getByTestId('effort-selector'));
    for (const key of ['off', 'low', 'medium', 'high']) {
      expect(screen.getByTestId(`menu-item-${key}`)).not.toBeDisabled();
    }
    // 切到 coder-plus：能力集仅 [high] → off/low/medium 置灰
    act(() => {
      useModelsStore.getState().select({ model: 'mock/qwen/qwen3-coder-plus', effort: 'high' });
    });
    await user.click(screen.getByTestId('effort-selector')); // 关闭再打开刷新
    await user.click(screen.getByTestId('effort-selector'));
    expect(screen.getByTestId('menu-item-off')).toBeDisabled();
    expect(screen.getByTestId('menu-item-low')).toBeDisabled();
    expect(screen.getByTestId('menu-item-medium')).toBeDisabled();
    expect(screen.getByTestId('menu-item-high')).not.toBeDisabled();
  });

  it('选择档位 → PATCH reasoning_effort；触发钮显示中文档位名', async () => {
    const user = userEvent.setup();
    requestLog.length = 0;
    seedModels();
    render(<EffortSelector sessionId={SESSION} />);
    expect(screen.getByTestId('effort-selector')).toHaveTextContent('中');

    await user.click(screen.getByTestId('effort-selector'));
    await user.click(screen.getByTestId('menu-item-high'));
    await waitFor(() => {
      const patch = requestLog.find((entry) => entry.method === 'PATCH');
      expect(patch?.body).toMatchObject({ reasoning_effort: 'high' });
    });
    expect(screen.getByTestId('effort-selector')).toHaveTextContent('最高');
  });

  it('列表未含当前模型（会话回声先到）：不伪造能力面，档位不置灰', async () => {
    const user = userEvent.setup();
    seedModels();
    render(<EffortSelector sessionId={null} />);
    act(() => {
      useModelsStore.getState().select({ model: 'unknown/model', effort: 'medium' });
    });
    await user.click(screen.getByTestId('effort-selector'));
    expect(screen.getByTestId('menu-item-off')).not.toBeDisabled();
    expect(screen.getByTestId('menu-item-high')).not.toBeDisabled();
  });
});
