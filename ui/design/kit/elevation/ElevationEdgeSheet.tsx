// Edges designed with elevation (cards, inputs, soft buttons, the borders that stay), a floating
// surface over busy content, the composer, and where the brand light comes back in Light. Specimens
// carry the same classes the shipped components do, so ./elevation.css draws them exactly as it
// would draw the product.
import { Check, Inbox, MoreHorizontal, Pencil, Plus, Send, Square, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

import { Section, Sheet, Specimen } from '../../frames/system/sheet';
import { useDraft } from '../draft';

const busy = Array.from({ length: 6 }, (_, i) => i);
// The shipped composer box (components/workbench/Composer.tsx).
const composer = 'composer-edge relative flex w-full items-end gap-1.5 rounded-2xl border py-2 pr-2 pl-3.5 shadow-composer focus-within:shadow-composer-focus data-working:shadow-composer-focus';

export function ElevationEdgeSheet() {
  const d = useDraft();
  return (
    <Sheet>
      <Section title={d('边与面 · 没有灰色实线边框', 'Edges · no opaque grey borders')} note={d('卡片靠底色 + 极淡的环（浅色带品牌色调的柔影，深色是白色细线 + 顶部高光）；输入框和次要按钮是浅底填充，聚焦出环', 'Cards: fill plus a faint ring; inputs and soft buttons are filled, focus draws a ring')}>
        <Specimen name={d('卡片', 'Card')} use="rounded-xl border-border → edge-card" className="w-56">
          <div className="w-56 rounded-xl border border-border bg-surface p-3">
            <div className="text-[13px] font-semibold">avibe</div>
            <div className="mt-1 text-[12px] text-muted">{d('修复中途断缓存 · 12 分钟前', 'Fix the cache break · 12 min ago')}</div>
          </div>
        </Specimen>
        <Specimen name={d('输入框 · 静止 / 聚焦', 'Input · rest / focus')} use="border-input → filled" className="w-56">
          <div className="flex w-56 flex-col gap-2">
            <Input defaultValue="avibe-docs" />
            <Input defaultValue="avibe-docs" className="ring-2 ring-ring" />
          </div>
        </Specimen>
        <Specimen name={d('次要按钮', 'Soft buttons')} use="outline / secondary → filled" className="w-56">
          <div className="flex h-[84px] w-56 items-center gap-2">
            <Button variant="outline" size="xs">
              <Check />
              {d('标记已读', 'Mark read')}
            </Button>
            <Button variant="secondary" size="xs">
              {d('取消', 'Cancel')}
            </Button>
          </div>
        </Specimen>
        <Specimen name={d('保留的边', 'Borders that stay')} use={d('选中、虚线「添加」、分隔线', 'selected, dashed add, dividers')} className="w-64">
          <div className="flex w-64 flex-col gap-2">
            <div className="rounded-lg border border-mint/40 bg-mint-soft px-3 py-2 text-[12px] font-medium">{d('选中', 'Selected')}</div>
            <div className="flex items-center gap-2 rounded-lg border border-dashed border-border-strong px-3 py-2 text-[12px] text-muted">
              <Plus className="size-3.5" />
              {d('添加伙伴', 'Add a Bot')}
            </div>
            <div className="divide-y divide-border rounded-lg text-[12px]">
              <div className="px-3 py-1.5">{d('分隔线', 'Divider')}</div>
              <div className="px-3 py-1.5">{d('分隔线', 'Divider')}</div>
            </div>
          </div>
        </Specimen>
      </Section>

      <Section title={d('浮层 · 盖在内容上', 'Floating · over content')} note={d('不透明，无描边；浅色阴影带品牌色调，深色有顶部高光', 'Opaque, no outline; Light shadows take the brand tint, Dark gets a top highlight')}>
        <Specimen name={d('菜单', 'Menu')} use=".motion-popover / role=menu" className="w-[420px]">
          <div className="relative h-40 w-[420px] overflow-hidden rounded-xl bg-background p-3">
            <div className="flex flex-col gap-1.5">
              {busy.map((i) => (
                <div key={i} className="flex items-center gap-2 text-[12px] text-foreground">
                  <span className={i % 2 ? 'size-3 rounded-full bg-cyan' : 'size-3 rounded-full bg-mint'} />
                  {d('已经读完现有 Web UI 代码，收件箱、项目各自一个入口。', 'Read the Web UI code; Inbox and Projects each get an entry.')}
                </div>
              ))}
            </div>
            <div role="menu" className="absolute right-4 top-6 w-44 rounded-lg border border-border bg-surface-3 p-1 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]">
              <div className="flex h-8 items-center gap-2 rounded-md px-2 text-[13px]">
                <Pencil className="size-3.5 text-muted" />
                {d('重命名', 'Rename')}
              </div>
              <div className="flex h-8 items-center gap-2 rounded-md bg-foreground/[0.04] px-2 text-[13px]">
                <MoreHorizontal className="size-3.5 text-muted" />
                {d('复制链接', 'Copy link')}
              </div>
              <div className="flex h-8 items-center gap-2 rounded-md px-2 text-[13px] text-destructive-ink">
                <Trash2 className="size-3.5" />
                {d('删除', 'Delete')}
              </div>
            </div>
          </div>
        </Specimen>
      </Section>

      <Section title={d('主角与「活着」', 'Hero and alive')} note={d('输入框：渐变细边 + 柔影，聚焦时薄荷环；Agent 工作时一道光沿边框流动（减少动态时静止）', 'Composer: gradient hairline and a soft edge, mint on focus; a light sweeps the edge while an Agent works (still under reduced motion)')}>
        <Specimen name={d('输入框', 'Composer')} use={d('静止', 'rest')} className="w-[340px]">
          <div className={composer}>
            <span className="flex-1 py-2 text-[13px] text-muted">{d('和歪比说…', 'Message the Bot…')}</span>
            <span className="grid size-9 place-items-center rounded-lg bg-primary text-primary-foreground">
              <Send className="size-4" />
            </span>
          </div>
        </Specimen>
        <Specimen name={d('输入框 · Agent 工作中', 'Composer · Agent working')} use={d('流光（截图是静止的一帧）', 'sweep (a still frame)')} className="w-[340px]">
          <div className={composer} data-working>
            <span className="flex-1 py-2 text-[13px] text-muted">{d('歪比正在忙，有话可以接着说…', 'The Bot is busy; keep typing…')}</span>
            <button type="button" className="grid size-9 place-items-center rounded-lg border border-pink/45 bg-pink/15 text-pink-ink">
              <Square className="size-4" />
            </button>
          </div>
        </Specimen>
      </Section>

      <Section title={d('光 · 浅色里换成品牌色柔影', 'Light · a tinted soft shadow in Light')} note={d('品牌主按钮、选中伙伴、状态点保持现状；未读卡和装饰图标块在浅色里是同色柔影，深色不变', 'CTA, selected Bot and dots unchanged; in Light, unread cards and decorative tiles get a same-hue soft shadow')}>
        <Specimen name={d('未读卡', 'Unread card')} use={d('浅色：同色柔影', 'Light: same-hue soft shadow')} className="w-64">
          <div className="flex w-64 flex-col gap-1.5 rounded-xl border border-mint/30 bg-mint/[0.05] p-3 shadow-glow-md-mint">
            <div className="text-[13px] font-semibold">{d('首席安全官', 'Security lead')}</div>
            <div className="text-[12px] text-muted">{d('发现 2 个高危漏洞，要我开 PR 吗？', 'Found 2 critical issues; open a PR?')}</div>
          </div>
        </Specimen>
        <Specimen name={d('装饰图标块', 'Decorative tile')} use={d('深色：光晕；浅色：同色柔影', 'Dark: glow; Light: soft shadow')} className="w-48">
          <div className="flex h-[76px] items-center">
            <div className="flex size-12 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint-ink shadow-glow-md-mint">
              <Inbox className="size-5" />
            </div>
          </div>
        </Specimen>
        <Specimen name={d('品牌主按钮', 'Brand CTA')} use={d('不变', 'unchanged')} className="w-40">
          <div className="flex h-[76px] items-center">
            <Button variant="brand" size="sm">
              {d('新建 Agent', 'New Agent')}
            </Button>
          </div>
        </Specimen>
      </Section>
    </Sheet>
  );
}
