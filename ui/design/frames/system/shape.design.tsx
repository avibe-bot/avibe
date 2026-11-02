// Shape and light: the radius scale, the elevation shadows, the card washes, and the accent glow
// scale of `src/index.css`, each drawn with the utility a component writes. A glow's geometry and
// colour live in the token; `glowScale.test.mjs` holds the scale to its rule.
import { cn } from '@/lib/utils';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const radii = [
  ['rounded-xs', '暂无组件使用'],
  ['rounded-sm', '命令列表项'],
  ['rounded-md', '输入框、弹出层、分段控件'],
  ['rounded-lg', '按钮、菜单、提示'],
  ['rounded-xl', '卡片、对话框'],
  ['rounded-2xl', '手机底部面板'],
  ['rounded-3xl', '暂无组件使用'],
  ['rounded-full', '徽标、开关、头像'],
] as const;

const elevations = [
  ['shadow-sm', '输入框'],
  ['shadow-md', '弹出层'],
  ['shadow-lg', '菜单、提示条'],
  ['shadow-xl', '对话框、生命周期弹层'],
  ['shadow-2xl', '手机底部面板'],
] as const;

const washes = [
  ['shadow-mint-card-sm', '卡片薄光'],
  ['shadow-mint-card', '卡片光'],
  ['shadow-mint-card-lg', '主卡片光'],
] as const;

// The sized rungs, smallest first: blur 12 → 48, spread a quarter of the blur.
const sized = [
  ['shadow-glow-xs-mint', 'xs · 控件轨道'],
  ['shadow-glow-xs-cyan', 'xs'],
  ['shadow-glow-sm-mint', 'sm · 图标块、侧栏标志'],
  ['shadow-glow-sm-cyan', 'sm'],
  ['shadow-glow-sm-violet', 'sm'],
  ['shadow-glow-sm-gold', 'sm'],
  ['shadow-glow-md-mint', 'md · 眉标、悬停标志'],
  ['shadow-glow-md-cyan', 'md'],
  ['shadow-glow-md-violet', 'md'],
  ['shadow-glow-md-gold', 'md'],
  ['shadow-glow-lg-mint', 'lg · 主按钮'],
  ['shadow-glow-lg-cyan', 'lg'],
  ['shadow-glow-lg-violet', 'lg'],
  ['shadow-glow-xl-mint', 'xl · 欢迎页标志'],
] as const;

// Roles named for their use rather than a size.
const roles = [
  ['shadow-glow-cta-mint', 'cta · brand 按钮，光晕随主题变宽'],
  ['shadow-glow-cta-cyan', 'cta'],
  ['shadow-glow-cta-violet', 'cta'],
  ['shadow-glow-cta-gold', 'cta'],
  ['shadow-glow-nav-mint', 'nav · 侧栏选中行'],
  ['shadow-glow-onboarding-mint', 'onboarding · 引导卡片悬停'],
] as const;

const dots = [
  ['bg-mint shadow-glow-dot-mint', 'shadow-glow-dot-mint', '运行中'],
  ['bg-gold shadow-glow-dot-gold', 'shadow-glow-dot-gold', '需要注意'],
  ['bg-destructive shadow-glow-dot-destructive', 'shadow-glow-dot-destructive', '出错'],
] as const;

// `wire` is the drop-shadow role: the Model Hub's connection lines glow through a filter.
const wires = [
  ['bg-mint [filter:drop-shadow(var(--shadow-glow-wire-mint))]', '--shadow-glow-wire-mint'],
  ['bg-cyan [filter:drop-shadow(var(--shadow-glow-wire-cyan))]', '--shadow-glow-wire-cyan'],
  ['bg-violet [filter:drop-shadow(var(--shadow-glow-wire-violet))]', '--shadow-glow-wire-violet'],
] as const;

const box = 'size-20 border border-border bg-card';
const glowTile = 'size-14 rounded-xl border border-border bg-card';

const shapeSheet = () => (
  <Sheet>
    <Section title="圆角">
      {radii.map(([className, use]) => (
        <Specimen key={className} name={className} use={use} className="w-32">
          <div className={cn(box, 'bg-surface-2', className)} />
        </Specimen>
      ))}
    </Section>
    <Section title="投影" note="浮在内容上方的层">
      {elevations.map(([className, use]) => (
        <Specimen key={className} name={className} use={use} className="w-36">
          <div className={cn('h-20 w-28 rounded-xl border border-border bg-card', className)} />
        </Specimen>
      ))}
      {washes.map(([className, use]) => (
        <Specimen key={className} name={className} use={use} className="w-40">
          <div className={cn('h-20 w-28 rounded-xl border border-border bg-card', className)} />
        </Specimen>
      ))}
    </Section>
  </Sheet>
);

const glowSheet = () => (
  <Sheet>
    <Section title="光晕阶梯" note="居中、扩散为模糊的四分之一，颜色在两个主题下都是暗色霓虹">
      {sized.map(([className, use]) => (
        <Specimen key={className} name={className} use={use} className="w-44">
          <div className={cn(glowTile, className)} />
        </Specimen>
      ))}
    </Section>
    <Section title="按用途命名的光晕">
      {roles.map(([className, use]) => (
        <Specimen key={className} name={className} use={use} className="w-52">
          <div className={cn(glowTile, className)} />
        </Specimen>
      ))}
    </Section>
    <Section title="状态点与连线" note="dot 不扩散；wire 用在 drop-shadow 里，颜色跟随主题">
      {dots.map(([className, name, use]) => (
        <Specimen key={name} name={name} use={use} className="w-52">
          <div className="flex h-14 items-center">
            <span className={cn('size-2 rounded-full', className)} />
          </div>
        </Specimen>
      ))}
      {wires.map(([className, name]) => (
        <Specimen key={name} name={name} use="模型网关连线" className="w-52">
          <div className="flex h-14 items-center">
            <span className={cn('h-0.5 w-40 rounded-full', className)} />
          </div>
        </Specimen>
      ))}
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'radius-shadows', title: '形状 · 圆角与投影', width: 1280, height: 620, status: 'shipped', fixtures: systemFixtures(), render: shapeSheet },
  { name: 'glows', title: '形状 · 光晕', width: 1280, height: 1000, status: 'shipped', fixtures: systemFixtures(), render: glowSheet },
]);

export default designFrames;
