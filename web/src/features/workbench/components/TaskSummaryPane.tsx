/**
 * 任务名标签内容（F25/R3§G；来源 design/prototype/side-panels.html tp-任务名，
 * 样式类 components/side-panels.css 占位块族）。
 *
 * round3 观察到右栏标签条首位为「任务名 tab」，tab 内容未深挖——本块为任务概要/
 * 调用轨迹占位（静态），诚实性口径（F15）：标题取真实会话名，概要正文/徽标计数/
 * 调用轨迹时间线均不伪造，待实测后补真形。
 */
export function TaskSummaryPane({ title }: { title: string }) {
  return (
    <section data-testid="task-summary-pane" aria-label="任务概要" className="flex h-full min-h-0 flex-col">
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        <div className="tlabel">任务概要</div>
        <div className="tcard">
          <div className="ttitle">{title}</div>
          {/* 概要正文骨架占位（真实概要生成待实测） */}
          <div className="tskl" aria-hidden="true">
            <i className="m" />
            <i className="s" />
          </div>
          <div className="tbadges">
            <span className="badge">任务名 · F25 占位</span>
          </div>
        </div>
        <div className="tlabel">调用轨迹</div>
        <div className="tph">调用轨迹 · 时间线占位（轨迹色系 tokens 已预留，未出稿）</div>
      </div>
      <div className="note mx-[15px] mb-3.5 flex-none">
        <b>任务名 tab（F25）· 待实测</b> — round3 观察到右栏标签条首位为任务名 tab（tablist：
        搜索｜任务名｜审查 ×｜＋ 新增｜收起）；tab 内容未深挖，本块为任务概要/调用轨迹占位，
        待实测后补真形。
      </div>
    </section>
  );
}
