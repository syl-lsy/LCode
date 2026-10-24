import { useState } from 'react';
import { useNavigate } from 'react-router';

// 新手引导页页面级样式（来源 design/prototype/onboarding.html）
import './onboarding.css';

/**
 * 新手引导页（B7，对照 design/prototype/onboarding.html 1:1）：
 *   3 步静态演示——顶部三段进度条（F27）+ 12 项双列选项卡（勾选态）+
 *   UI 模式双选卡 + 助手偏好占位步；本地 state 翻步。
 *
 * LCode 裁剪决策：完成/退出仅记 localStorage「已完成引导」（lcode.onboarding.done）
 * 并回首页——首页不强制跳转引导，本页不接管导航；直接 URL（/onboarding）可达。
 * 独立全屏页，不入 AppShell 三栏。
 */

type OnboardingStep = 1 | 2 | 3;

/** 步骤名（round1 §19；F27 移入进度条 aria-label/title 保留） */
const STEP_NAMES: Record<OnboardingStep, string> = {
  1: '工作方向',
  2: 'UI 模式',
  3: '助手偏好',
};

/** 第 1 步 12 项工作方向选项（onboarding.html 原文顺序与文案） */
const JOB_OPTIONS: readonly string[] = [
  '软件开发/数据/AI',
  '创业/自由职业/OPC',
  '测试/运维/安全',
  '产品/项目/解决方案',
  'UI/UX/视觉设计',
  '学生/教师/科研',
  '金融/财务/咨询',
  '自媒体/内容创作',
  '运营/电商/客户服务',
  '市场营销/品牌/公关',
  '法律/行政/人力资源',
  '其他职业',
];

/** 第 2 步界面模式双选卡（onboarding.html 原文文案；默认选中「编程模式」） */
const MODE_OPTIONS: readonly { readonly title: string; readonly desc: string }[] = [
  {
    title: '编程模式',
    desc: '我需要查看代码、命令输出和修改细节，掌握完整的开发过程。',
  },
  {
    title: '办公模式',
    desc: '我更关注任务进度和结果，不需要查看代码、命令行等技术细节。',
  },
];

/** 「已完成引导」持久化键（lcode. 前缀与 uiStore 持久化约定一致） */
const ONBOARDING_DONE_KEY = 'lcode.onboarding.done';

/** 写「已完成引导」（隐私模式等持久化失败静默降级，不阻断完成动作）。 */
function markOnboardingDone(): void {
  try {
    localStorage.setItem(ONBOARDING_DONE_KEY, '1');
  } catch {
    // ignore：持久化失败不阻断导航
  }
}

export function OnboardingView() {
  const navigate = useNavigate();
  const [step, setStep] = useState<OnboardingStep>(1);
  /** 第 1 步单选项（原型默认勾选「软件开发/数据/AI」） */
  const [job, setJob] = useState('软件开发/数据/AI');
  /** 第 2 步单选项（原型默认勾选「编程模式」） */
  const [mode, setMode] = useState('编程模式');

  const finish = (): void => {
    markOnboardingDone();
    navigate('/');
  };

  return (
    /* 原型 body stretch 布局的页面根等价 */
    <div className="flex min-h-screen items-stretch">
      {/* .main 与 app-shell.css 同名复用（语义一致），内联补本页 padding/overflow（见 onboarding.css 头注释） */}
      <main className="main scroll-thin" style={{ padding: '22px 34px', overflowY: 'auto' }}>
        <div className="top">
          {/* F27（R3§J）：顶部三段进度条；步骤名移入 aria-label 保留 */}
          <div
            className="prog"
            data-testid="onboarding-progress"
            role="progressbar"
            aria-valuemin={1}
            aria-valuemax={3}
            aria-valuenow={step}
            aria-label={`引导进度：第 ${step} 步 · ${STEP_NAMES[step]}`}
          >
            {([1, 2, 3] as const).map((index) => (
              <span
                key={index}
                className={`seg${index === step ? ' on' : ''}${index < step ? ' done' : ''}`}
                title={`第 ${index} 步 · ${STEP_NAMES[index]}`}
              />
            ))}
          </div>
          <button
            type="button"
            className="exit"
            data-testid="onboarding-exit"
            title="退出引导，返回首页"
            onClick={() => navigate('/')}
          >
            退出引导
          </button>
        </div>

        <div className="inner">
          {/* 第 1 步：工作方向（F27：12 项双列 + 勾选态，默认勾选「软件开发/数据/AI」） */}
          <section data-testid="onboarding-step-1" hidden={step !== 1}>
            <h1>你的主要工作方向是？</h1>
            <p className="lead">选择最接近你日常工作的一项，让 LCode 更懂你的工作。</p>
            <div className="cards" data-testid="onboarding-jobs">
              {JOB_OPTIONS.map((label) => (
                <button
                  key={label}
                  type="button"
                  className="oc"
                  aria-pressed={job === label}
                  onClick={() => setJob(label)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="foot">
              <button type="button" className="btn" onClick={() => setStep(2)}>
                跳过
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setStep(2)}>
                下一步
              </button>
            </div>
          </section>

          {/* 第 2 步：UI 模式 */}
          <section data-testid="onboarding-step-2" hidden={step !== 2}>
            <h1>你偏好哪种界面模式？</h1>
            <p className="lead">模式决定信息呈现的详略，之后可随时在设置中更改。</p>
            <div className="cards" style={{ gridTemplateColumns: '1fr 1fr' }} data-testid="onboarding-modes">
              {MODE_OPTIONS.map((item) => (
                <button
                  key={item.title}
                  type="button"
                  className="oc"
                  aria-pressed={mode === item.title}
                  onClick={() => setMode(item.title)}
                >
                  <b>{item.title}</b>
                  <span>{item.desc}</span>
                </button>
              ))}
            </div>
            <div className="foot">
              <button type="button" className="btn" onClick={() => setStep(1)}>
                上一步
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setStep(3)}>
                下一步
              </button>
            </div>
          </section>

          {/* 第 3 步：助手偏好（占位：LCode 按官方结构保留步骤位，具体偏好项待 PRD 细化） */}
          <section data-testid="onboarding-step-3" hidden={step !== 3}>
            <h1>选择你的工作助手偏好</h1>
            <p className="lead">第 3 步为占位：LCode 按官方结构保留步骤位，具体偏好项待 PRD 细化。</p>
            {/* 原型即内联样式书写（无类定义），按原型内联 style 等价保留 */}
            <div
              style={{
                minHeight: 220,
                border: '1px dashed var(--color-border-hover)',
                borderRadius: 'var(--radius-lg)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                marginBottom: 26,
              }}
            >
              <span className="mono" style={{ color: 'var(--color-foreground-subtlest)', fontSize: 'var(--text-ui-sm)' }}>
                步骤 3 · 助手偏好（占位）
              </span>
            </div>
            <div className="foot">
              <button type="button" className="btn" onClick={() => setStep(2)}>
                上一步
              </button>
              <button type="button" className="btn btn-primary" data-testid="onboarding-finish" onClick={finish}>
                完成，进入 LCode
              </button>
            </div>
          </section>
        </div>
      </main>

      {/* 右侧宣传面板（§19 complementary） */}
      <aside className="aside">
        <h2>简单、迅捷、氛围十足！</h2>
        <p>多智能体协作完成复杂目标，随时随地尽在掌控。</p>
        <div className="deco" aria-hidden="true">
          <i />
          <i />
          <i />
        </div>
      </aside>
    </div>
  );
}
