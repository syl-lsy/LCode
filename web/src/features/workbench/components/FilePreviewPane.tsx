import { Fragment } from 'react';

import { Icon } from '@/components/Icon';

/**
 * 文件预览标签（changed-files.html 右栏「文件预览」；P0 只读占位）。
 *
 * - 结构 = .crumb 面包屑（root / 目录段… / 当前文件）+ .v-body 只读正文；
 *   标签条不在此渲染——右栏功能标签由 PanelTabs 统一承担（R10：文件预览是右侧
 *   公用面板中的一个标签，与审查/终端共用同一条标签条）；
 * - 「在编辑器中打开」保持 disabled——LCode 无本地编辑器语义，P2 桌面壳再启用
 *   （原型同款占位口径）；「更多」钮同为止占位；
 * - 正文为只读占位说明（真实文件内容读取延后，不伪造文件内容——F15 诚实性口径）；
 * - 打开入口：消息流改动文件卡「选择打开方式」（DiffCard，placeholder 标签
 *   `file-preview:<path>`）。
 */

/** 路径分段（过滤空段；绝对路径的空首段由 root 起始段语义吸收）。 */
function segmentsOf(path: string): readonly string[] {
  return path.split('/').filter((segment) => segment !== '');
}

export function FilePreviewPane({ path }: { path: string }) {
  const segments = segmentsOf(path);
  return (
    <section
      data-testid="file-preview-pane"
      aria-label="文件预览"
      className="flex h-full min-h-0 flex-col"
    >
      <div className="crumb">
        <span className="c">root</span>
        {segments.map((segment, index) => (
          <Fragment key={`${segment}-${index}`}>
            <span className="sep" aria-hidden="true">
              /
            </span>
            <span className={`c ${index === segments.length - 1 ? 'cur' : ''}`}>{segment}</span>
          </Fragment>
        ))}
        <span className="grow" aria-hidden="true" />
        <button
          type="button"
          className="icon-btn flex-none"
          title="更多操作（P2）"
          aria-disabled="true"
          disabled
          style={{ width: 22, height: 22 }}
        >
          <Icon name="more" size={11} />
        </button>
        <button
          type="button"
          className="btn btn-sm flex-none"
          disabled
          title="LCode 无本地编辑器语义，P2 桌面壳再启用"
        >
          在编辑器中打开
        </button>
      </div>
      <div className="v-body scroll-thin md">
        <h1>{segments.at(-1) ?? '文件预览'}</h1>
        <p className="text-[length:var(--text-ui-sm)] text-foreground-subtle">
          文件预览为只读占位（P0）：沙箱内文件内容的读取与渲染将在后续里程碑接入，
          此处不展示未核实的文件内容。
        </p>
        <p className="text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
          「在编辑器中打开」保持 disabled —— LCode 无本地编辑器语义，P2 桌面壳再启用。
        </p>
      </div>
    </section>
  );
}
