// The color roles of `src/index.css`, each drawn with the class that paints it, so a swatch is the
// variable's live value in the frame's theme. Values are never retyped here: read them in index.css.
import { cn } from '@/lib/utils';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Chip, Section, Sheet, Specimen } from './sheet';

const tile = 'h-16 w-36 rounded-lg border border-border';

const surfaces = [
  ['bg-background', '页面底色'],
  ['bg-surface', '面板、侧栏内容'],
  ['bg-surface-2', '卡片内层、悬停'],
  ['bg-surface-3', '凹陷区、代码块'],
  ['bg-card', '卡片、对话框'],
  ['bg-popover', '弹出层、菜单'],
  ['bg-secondary', '次要按钮'],
  ['bg-muted-soft', '弱化底色'],
  ['bg-[var(--sidebar-background)]', '侧栏'],
  ['bg-[var(--nav-hover-bg)]', '导航悬停'],
  ['bg-[var(--nav-selected-bg)]', '导航选中'],
  ['bg-[var(--logo-well-background)]', '标志底座'],
] as const;

const texts = [
  ['text-foreground', '正文、标题'],
  ['text-muted', '次要文字、说明'],
  ['text-card-foreground', '卡片上的文字'],
  ['text-popover-foreground', '弹出层上的文字'],
  ['text-secondary-foreground', '次要按钮文字'],
] as const;

const lines = [
  ['border-border', '分隔线、卡片描边'],
  ['border-border-strong', '强调描边、徽标'],
  ['border-input', '输入框描边'],
  ['border-[var(--nav-selected-border)]', '导航选中描边'],
  ['border-[var(--illustration-card-border)]', '插图描边'],
] as const;

// A brand fill, its hover fill (both carrying their paired label), and the same role as text.
const semantic = [
  { role: 'primary', fill: 'bg-primary text-primary-foreground', hover: 'bg-primary-hover text-primary-foreground', ink: 'text-primary-ink', use: '主操作、开关、选中' },
  { role: 'accent', fill: 'bg-accent text-accent-foreground', hover: 'bg-accent-hover text-accent-foreground', ink: 'text-accent-ink', use: '链接、信息、高亮' },
  { role: 'destructive', fill: 'bg-destructive text-destructive-foreground', hover: 'bg-destructive-hover text-destructive-foreground', ink: 'text-destructive-ink', use: '删除、错误' },
] as const;

// Each accent: its fill with the label printed on it, its hover fill, its wash with ink on it, and ink alone.
// Pink declares no label or hover: it is only ever a wash and its ink (the soft destructive button).
const accents = [
  { name: 'mint', fill: 'bg-mint text-primary-foreground', hover: 'bg-mint-hover text-primary-foreground', soft: 'bg-mint-soft text-mint-ink', ink: 'text-mint-ink' },
  { name: 'cyan', fill: 'bg-cyan text-accent-foreground', hover: 'bg-cyan-hover text-accent-foreground', soft: 'bg-cyan-soft text-cyan-ink', ink: 'text-cyan-ink' },
  { name: 'violet', fill: 'bg-violet text-violet-foreground', hover: 'bg-violet-hover text-violet-foreground', soft: 'bg-violet-soft text-violet-ink', ink: 'text-violet-ink' },
  { name: 'gold', fill: 'bg-gold text-gold-foreground', hover: 'bg-gold-hover text-gold-foreground', soft: 'bg-gold-soft text-gold-ink', ink: 'text-gold-ink' },
  { name: 'pink', fill: 'bg-pink', hover: null, soft: 'bg-pink-soft text-pink-ink', ink: 'text-pink-ink' },
] as const;

const surfacesSheet = () => (
  <Sheet>
    <Section title="底色" note="页面、面板、卡片与导航的填充">
      {surfaces.map(([className, use]) => (
        <Specimen key={className} name={className} use={use}>
          <div className={cn(tile, className, className.includes('nav-selected') && 'border-[var(--nav-selected-border)]')} />
        </Specimen>
      ))}
    </Section>
    <Section title="文字" note="放在卡片上">
      {texts.map(([className, use]) => (
        <Specimen key={className} name={className} use={use}>
          <div className={cn(tile, 'flex items-center bg-card px-3 text-[15px] font-medium', className)}>文字 Aa 123</div>
        </Specimen>
      ))}
    </Section>
    <Section title="线条与焦点环">
      {lines.map(([className, use]) => (
        <Specimen key={className} name={className} use={use}>
          <div className={cn('h-16 w-36 rounded-lg border-2 bg-card', className)} />
        </Specimen>
      ))}
      <Specimen name="ring-ring" use="键盘焦点环">
        <div className="h-16 w-36 rounded-lg border border-input bg-background ring-2 ring-ring" />
      </Specimen>
    </Section>
  </Sheet>
);

const accentsSheet = () => (
  <Sheet>
    <Section title="语义填充" note="填充配标签色；悬停换成声明的 hover 填充；作文字时用 ink">
      {semantic.map(({ role, fill, hover, ink, use }) => (
        <div key={role} className="flex flex-col gap-2">
          <div className="flex gap-2">
            <Chip className={fill}>{role}</Chip>
            <Chip className={hover}>hover</Chip>
            <Chip className={cn('border border-border bg-card', ink)}>ink</Chip>
          </div>
          <span className="text-[11px] text-muted">
            <span className="font-mono text-foreground">--{role}</span> · {use}
          </span>
        </div>
      ))}
    </Section>
    <Section title="品牌色" note="每个强调色：填充、悬停、浅底（soft）与文字（ink）">
      {accents.map(({ name, fill, hover, soft, ink }) => (
        <Specimen key={name} name={`--${name}`}>
          <div className="flex flex-col gap-2">
            <Chip className={fill}>{name === 'pink' ? null : name}</Chip>
            {hover ? <Chip className={hover}>hover</Chip> : <Chip className="border border-dashed border-border text-muted">无 hover</Chip>}
            <Chip className={soft}>soft</Chip>
            <Chip className={cn('border border-border bg-card', ink)}>ink</Chip>
          </div>
        </Specimen>
      ))}
      <Specimen name="--route-manual" use="手动路由">
        <Chip className="border border-border bg-card text-[var(--route-manual)]">手动</Chip>
      </Specimen>
    </Section>
  </Sheet>
);

const gradients = [
  ['page-glow-console', '工作台'],
  ['page-glow-settings', '设置'],
  ['page-glow-wizard-welcome', '旧引导 · 欢迎（暂无页面使用）'],
  ['page-glow-wizard-backends', '旧引导 · 后端（暂无页面使用）'],
  ['page-glow-wizard-platforms', '旧引导 · 平台（暂无页面使用）'],
  ['page-glow-wizard-slack', '旧引导 · Slack（暂无页面使用）'],
  ['page-glow-wizard-summary', '旧引导 · 完成（暂无页面使用）'],
] as const;

// The utilities fix the glow to the viewport; a tile paints the same variable on itself instead.
const gradientTile: Record<(typeof gradients)[number][0], string> = {
  'page-glow-console': '[background:var(--gradient-console)]',
  'page-glow-settings': '[background:var(--gradient-settings)]',
  'page-glow-wizard-welcome': '[background:var(--gradient-wizard-welcome)]',
  'page-glow-wizard-backends': '[background:var(--gradient-wizard-backends)]',
  'page-glow-wizard-platforms': '[background:var(--gradient-wizard-platforms)]',
  'page-glow-wizard-slack': '[background:var(--gradient-wizard-slack)]',
  'page-glow-wizard-summary': '[background:var(--gradient-wizard-summary)]',
};

const gradientsSheet = () => (
  <Sheet>
    <Section title="页面光晕" note="铺在页面滚动容器上，随主题换透明度">
      {gradients.map(([utility, use]) => (
        <Specimen key={utility} name={utility} use={use}>
          <div className={cn('h-40 w-64 rounded-xl border border-border', gradientTile[utility])} />
        </Specimen>
      ))}
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'surfaces', title: '颜色 · 底色、文字、线条', width: 1280, height: 700, status: 'shipped', fixtures: systemFixtures(), render: surfacesSheet },
  { name: 'accents', title: '颜色 · 语义填充与品牌色', width: 1280, height: 560, status: 'shipped', fixtures: systemFixtures(), render: accentsSheet },
  { name: 'gradients', title: '颜色 · 页面光晕', width: 1280, height: 540, status: 'shipped', fixtures: systemFixtures(), render: gradientsSheet },
]);

export default designFrames;
