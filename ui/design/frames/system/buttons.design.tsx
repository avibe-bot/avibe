// Button (`components/ui/button.tsx`, recipe in `button-variants.ts`): every variant at rest, with an
// icon, and disabled; every size; and CopyButton, the one copy control built on it.
import { ArrowRight, Download, Plus, RotateCw, Trash2 } from 'lucide-react';
import type { ComponentProps, ReactNode } from 'react';

import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

type Variant = NonNullable<ComponentProps<typeof Button>['variant']>;
type Size = NonNullable<ComponentProps<typeof Button>['size']>;

const variants: { variant: Variant; label: string; icon: ReactNode; use: string }[] = [
  { variant: 'default', label: '保存', icon: <Plus />, use: '主操作' },
  { variant: 'brand', label: '开始使用', icon: <ArrowRight />, use: '品牌 CTA，带光晕' },
  { variant: 'brand-cyan', label: '连接平台', icon: <Plus />, use: '品牌 CTA · 青' },
  { variant: 'brand-gold', label: '立即升级', icon: <Download />, use: '品牌 CTA · 金' },
  { variant: 'brand-violet', label: '重启', icon: <RotateCw />, use: '品牌 CTA · 紫' },
  { variant: 'secondary', label: '取消', icon: <Plus />, use: '次要操作' },
  { variant: 'outline', label: '导出日志', icon: <Download />, use: '描边，铺在光晕页面上' },
  { variant: 'outline-cyan', label: '阅读文档', icon: <ArrowRight />, use: '文档类入口' },
  { variant: 'accent', label: '运行检查', icon: <RotateCw />, use: '青色浅底' },
  { variant: 'ghost', label: '更多', icon: <Plus />, use: '工具栏、行内' },
  { variant: 'destructive', label: '删除', icon: <Trash2 />, use: '不可撤销的删除' },
  { variant: 'destructive-soft', label: '移除', icon: <Trash2 />, use: '面板内的删除' },
  { variant: 'link', label: '了解更多', icon: null, use: '文字链接' },
];

const sizes: { size: Size; use: string }[] = [
  { size: '2xs', use: 'h-6 · 通知旁的小按钮' },
  { size: 'xs', use: 'h-8 · 工具栏' },
  { size: 'sm', use: 'h-9 · 行内 CTA' },
  { size: 'default', use: 'h-10 · 默认' },
  { size: 'lg', use: 'h-12 · 主要 CTA' },
  { size: 'hero', use: 'h-[52px] · 欢迎页' },
];

const variantsSheet = () => (
  <Sheet>
    <Section title="变体" note="静止、带图标、禁用" className="grid grid-cols-2 gap-x-12 gap-y-5">
      {variants.map(({ variant, label, icon, use }) => (
        <div key={variant} className="flex items-center gap-4">
          <div className="flex w-36 flex-col gap-0.5">
            <span className="font-mono text-[11px] text-foreground">{variant}</span>
            <span className="text-[11px] text-muted">{use}</span>
          </div>
          <Button variant={variant}>{label}</Button>
          {icon && <Button variant={variant}>{icon}{label}</Button>}
          <Button variant={variant} disabled>{label}</Button>
        </div>
      ))}
    </Section>
  </Sheet>
);

const sizesSheet = () => (
  <Sheet>
    <Section title="尺寸" className="items-end gap-6">
      {sizes.map(({ size, use }) => (
        <Specimen key={size} name={size} use={use}>
          <div className="flex items-center gap-2">
            <Button size={size}><Plus />新建会话</Button>
            <Button size={size} variant="secondary">取消</Button>
          </div>
        </Specimen>
      ))}
      <Specimen name="icon" use="h-9 w-9 · 图标按钮，aria-label 同时是悬停提示">
        <div className="flex items-center gap-2">
          <Button size="icon" variant="ghost" aria-label="刷新"><RotateCw /></Button>
          <Button size="icon" variant="secondary" aria-label="新建"><Plus /></Button>
        </div>
      </Specimen>
    </Section>
    <Section title="复制按钮" note="CopyButton：默认 secondary · xs，确认在按钮内显示">
      <Specimen name="CopyButton" use="带文字">
        <CopyButton value="vibe status" />
      </Specimen>
      <Specimen name="CopyButton label={null}" use="只有图标">
        <CopyButton value="vibe status" label={null} />
      </Specimen>
      <Specimen name='variant="ghost" size="2xs"' use="代码块、命令旁">
        <CopyButton value="vibe status" variant="ghost" size="2xs" />
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'variants', title: '按钮 · 变体', width: 1280, height: 520, status: 'shipped', fixtures: systemFixtures(), render: variantsSheet },
  { name: 'sizes', title: '按钮 · 尺寸与复制按钮', width: 1280, height: 420, status: 'shipped', fixtures: systemFixtures(), render: sizesSheet },
]);

export default designFrames;
