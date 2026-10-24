import { useEffect, type ReactNode } from 'react';

import { listModels } from '@/api/models';
import { patchSession } from '@/api/sessions';
import { ApiError } from '@/api/http';

import { useModelsStore } from '../store';

import { Icon } from '@/components/Icon';
import { Menu, type MenuItem } from '@/components/Menu';

/**
 * 模型选择器（A4 §8.1 ModelSelector 行；F8 单层菜单；AC-014-1/2）。
 *
 * 两旋钮之一——UI 只暴露模型选择 + reasoning-effort 档位，不暴露采样参数
 * （AGENTS.md 硬约束 / REQ-014）。热切换：选中即 PATCH /sessions/{id}
 * （下条消息生效，AC-014-2），以服务端回声 syncFromSession 为准；sessionId 为空
 * （首页）只更新本地选择，待建会话时随 SessionCreate 携带。
 *
 * F8 单层菜单（toolbar-menus 菜单 1 一比一）：当前 provider 模型平铺
 * （选中勾最前 + 徽标），其他 provider 折叠为 ▸ 子菜单项（P1 模型管理接入前
 * 置灰占位），底部「管理模型」占位禁用。
 */

/** 模型能力徽标（原型口径：coder 模型带 coder 徽标；chat/视觉无契约字段不伪造）。 */
function modelBadge(displayName: string): ReactNode {
  return displayName.includes('coder') ? <span className="badge">coder</span> : null;
}

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
        className="tool-btn mono"
        aria-label="模型选择器（加载中）"
      >
        模型…
      </button>
    );
  }

  const current = models.find((m) => m.id === selected.model);
  const currentProvider = current?.provider ?? '';

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

  // F8：当前 provider 模型平铺 + 其他 provider 折叠为 ▸ 子菜单项（保持列表顺序去重）
  const providerModels = models.filter((m) => m.provider === currentProvider);
  const otherProviders = [
    ...new Set(models.filter((m) => m.provider !== currentProvider).map((m) => m.provider)),
  ];

  const items: MenuItem[] = [
    {
      key: 'provider-head',
      kind: 'header',
      label: (
        <>
          <span className="mono">{currentProvider}</span>
          {/* provider 档位徽标（models.html 套餐行的浮层缩略；P2 计费前为静态演示值） */}
          <span className="badge">个人</span>
        </>
      ),
    },
    ...providerModels.map((model) => ({
      key: model.id,
      label: <span className="nm mono">{model.display_name}</span>,
      trailing: modelBadge(model.display_name),
      checkFirst: true,
      selected: model.id === selected.model,
      onSelect: () => selectModel(model.id),
    })),
    ...(otherProviders.length > 0
      ? ([
          { key: 'sep-providers', kind: 'separator' },
          ...otherProviders.map((provider) => ({
            key: `provider-${provider}`,
            label: <span className="nm mono">{provider}</span>,
            trailing: <Icon name="chevron-right" size={12} className="arw" />,
            disabled: true,
            title: '该 provider 模型列表（▸ 子菜单 P1 模型管理接入前占位）',
          })),
        ] satisfies MenuItem[])
      : []),
    { key: 'sep-manage', kind: 'separator' },
    {
      key: 'manage-models',
      leading: <Icon name="settings" size={12} className="text-foreground-subtlest" />,
      label: '管理模型',
      disabled: true,
      title: '模型管理页（models.html，P1 占位）',
    },
  ];

  return (
    <Menu
      trigger={
        <>
          {/* 原型口径：触发钮 mono 显示 provider / 模型路径（toolbar-menus 菜单 1 触发器） */}
          <span>
            {currentProvider} / {current?.display_name ?? selected.model}
          </span>
          <Icon name="chevron-down" size={10} className="chev" />
        </>
      }
      triggerClassName="tool-btn mono"
      triggerLabel="模型选择器"
      triggerTestId="model-selector"
      menuLabel="选择模型"
      direction="up"
      align="left"
      items={items}
    />
  );
}
