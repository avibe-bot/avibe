// Type: the three families `src/index.css` declares, and the size ladder the product's components
// set with Tailwind classes. Each row is drawn with the class string its component uses.
import { cn } from '@/lib/utils';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const families = [
  ['font-sans', 'Inter：界面文字'],
  ['font-mono', 'JetBrains Mono：路径、模型名、密钥、眉标'],
  ['font-display', 'Inter：h1–h6 标题'],
] as const;

const familyClass: Record<(typeof families)[number][0], string> = {
  'font-sans': 'font-sans',
  'font-mono': 'font-mono',
  'font-display': 'font-display',
};

// Largest first: the class string a component sets, a sample, and where the product uses it.
const ladder = [
  ['text-[28px] font-bold leading-tight tracking-[-0.4px]', '设置页标题', '设置页标题'],
  ['text-[24px] font-bold', '工作台页标题', '工作台页面标题'],
  ['text-lg font-semibold leading-tight', '对话框标题', 'DialogTitle'],
  ['text-[15px] font-semibold tracking-tight', '卡片标题', 'CardTitle'],
  ['text-sm', '输入框里的文字，对话框说明', 'Input、DialogDescription（14px）'],
  ['text-[13px]', '正文与按钮：让智能体住进你的电脑', 'Button、CardDescription、设置行'],
  ['text-[12.5px]', '菜单项：在新窗口打开', 'ContextMenuItem'],
  ['text-[12px]', '次要文字与小按钮：3 分钟前更新', 'Button xs、说明文字（text-xs 同为 12px）'],
  ['text-[11px] font-semibold tracking-wide', '徽标与元信息', 'Badge'],
  ['text-[10px] font-semibold uppercase tracking-[0.12em] text-muted', 'Agent 分组标签', '设置导航分组'],
] as const;

const weights = [
  ['font-normal', '400'],
  ['font-medium', '500'],
  ['font-semibold', '600'],
  ['font-bold', '700'],
] as const;

const typeSheet = () => (
  <Sheet>
    <Section title="字族" note="index.css 只声明字族；字号由组件上的 Tailwind 类决定">
      {families.map(([name, use]) => (
        <Specimen key={name} name={name} use={use} className="w-72">
          <div className={cn(familyClass[name], 'text-[20px] text-foreground')}>Avibe 智能体 0123</div>
        </Specimen>
      ))}
    </Section>
    <Section title="字号阶梯" note="从大到小，左侧是组件实际写的类" className="flex-col gap-3">
      {ladder.map(([className, sample, use]) => (
        <div key={className} className="grid w-full grid-cols-[22rem_1fr] items-baseline gap-6 border-b border-border pb-3">
          <div className="flex flex-col gap-0.5">
            <span className="font-mono text-[11px] text-foreground">{className}</span>
            <span className="text-[11px] text-muted">{use}</span>
          </div>
          <div className={cn('text-foreground', className)}>{sample}</div>
        </div>
      ))}
    </Section>
    <Section title="字重">
      {weights.map(([className, weight]) => (
        <Specimen key={className} name={className} use={weight} className="w-40">
          <div className={cn('text-[20px] text-foreground', className)}>字重 Aa</div>
        </Specimen>
      ))}
      <Specimen name="font-mono text-[11px] font-bold uppercase tracking-[0.14em]" use="眉标（Badge eyebrow）" className="w-80">
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-cyan-ink">Agent OS</div>
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'scale', title: '文字 · 字族、字号与字重', width: 1280, height: 1060, status: 'shipped', fixtures: systemFixtures(), render: typeSheet },
]);

export default designFrames;
