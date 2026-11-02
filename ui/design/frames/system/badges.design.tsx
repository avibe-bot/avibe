// Badges and the chips built on them: Badge (`components/ui/badge.tsx`, recipe in
// `badge-variants.ts`) in every variant, the brand EyebrowBadge and StatusPill, the Bot mention chip
// and avatar, and the citation badge with the card it opens on hover.
import { Check } from 'lucide-react';
import type { ComponentProps } from 'react';

import { Badge } from '@/components/ui/badge';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { BotMentionChip } from '@/components/ui/bot-mention-chip';
import { CitationBadge } from '@/components/ui/citation-badge';
import { EyebrowBadge } from '@/components/visual/EyebrowBadge';
import { StatusPill } from '@/components/visual/StatusPill';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

type Variant = NonNullable<ComponentProps<typeof Badge>['variant']>;

const variants: { variant: Variant; label: string; dot?: string; use: string }[] = [
  { variant: 'default', label: '默认', use: '中性标签' },
  { variant: 'secondary', label: '未检测', dot: 'bg-muted/60', use: '弱化、未知' },
  { variant: 'outline', label: 'v3.2.4', use: '版本、计数' },
  { variant: 'success', label: '可用', dot: 'bg-mint', use: '正常、已连接' },
  { variant: 'warning', label: '可升级', dot: 'bg-gold', use: '需要注意' },
  { variant: 'info', label: '更新中', dot: 'bg-cyan', use: '进行中、信息' },
  { variant: 'destructive', label: '未运行', dot: 'bg-destructive', use: '出错' },
];

const eyebrowTones = ['cyan', 'mint', 'violet', 'gold'] as const;
const pillTones = [
  ['running', '运行中'],
  ['warning', '等待批准'],
  ['stopped', '已停止'],
  ['idle', '空闲'],
] as const;

const citation = (index: number, label: string, title: string) => ({ index, label, title, url: `https://${label}/` });

const badgesSheet = () => (
  <Sheet>
    <Section title="Badge 变体" note="圆角胶囊，11px 半粗">
      {variants.map(({ variant, label, dot, use }) => (
        <Specimen key={variant} name={variant} use={use} className="w-32">
          <div className="flex flex-col items-start gap-2">
            <Badge variant={variant}>{label}</Badge>
            {dot && (
              <Badge variant={variant}>
                <span className={`size-2 rounded-full ${dot}`} />
                {label}
              </Badge>
            )}
          </div>
        </Specimen>
      ))}
      <Specimen name="recommendation" use="由调用方上色的推荐签" className="w-36">
        <Badge variant="recommendation" className="border-mint/40 bg-mint-soft text-mint-ink">推荐 · 默认</Badge>
      </Specimen>
      <Specimen name="eyebrow" use="眉标" className="w-40">
        <Badge variant="eyebrow">Agent OS</Badge>
      </Specimen>
    </Section>
    <Section title="品牌胶囊" note="components/visual">
      {eyebrowTones.map((tone) => (
        <Specimen key={tone} name={`EyebrowBadge tone="${tone}"`} className="w-48">
          <EyebrowBadge tone={tone}>Step 02</EyebrowBadge>
        </Specimen>
      ))}
      {pillTones.map(([tone, label]) => (
        <Specimen key={tone} name={`StatusPill tone="${tone}"`} className="w-48">
          <StatusPill tone={tone} label={label} />
        </Specimen>
      ))}
      <Specimen name="StatusPill indicator" use="自定义指示" className="w-48">
        <StatusPill tone="running" label="已完成" indicator={<Check className="size-3.5 text-mint-ink" />} />
      </Specimen>
    </Section>
    <Section title="伙伴" note="BotAvatar 与 @提及">
      <Specimen name="BotAvatar · mascot" use="歪比">
        <BotAvatar avatar={{ kind: 'mascot' }} name="歪比" size={40} />
      </Specimen>
      <Specimen name="BotAvatar · emoji" use="表情在自己的色相上">
        <div className="flex gap-2">
          <BotAvatar avatar={{ kind: 'emoji', emoji: '🛡️', hue: 200 }} name="首席安全官" size={40} />
          <BotAvatar avatar={{ kind: 'emoji', emoji: '🧭', hue: 42 }} name="产品经理" size={40} />
        </div>
      </Specimen>
      <Specimen name="BotAvatar · 默认" use="名字的首字">
        <div className="flex gap-2">
          <BotAvatar avatar={null} name="运营助理" size={40} />
          <BotAvatar avatar={null} name="Reviewer" size={40} />
        </div>
      </Specimen>
      <Specimen name="BotAvatar · 尺寸" use="16 / 24 / 32">
        <div className="flex items-end gap-2">
          <BotAvatar avatar={{ kind: 'mascot' }} name="歪比" size={16} />
          <BotAvatar avatar={{ kind: 'mascot' }} name="歪比" size={24} />
          <BotAvatar avatar={{ kind: 'mascot' }} name="歪比" size={32} />
        </div>
      </Specimen>
      <Specimen name="BotMentionChip" use="已知伙伴 / 已删除的伙伴">
        <div className="flex items-center gap-2 text-[13px]">
          <BotMentionChip handle="cso" fallback="@cso" />
          <BotMentionChip handle="pm" fallback="@pm" />
          <BotMentionChip handle="gone" fallback="@前任助理" />
        </div>
      </Specimen>
    </Section>
    <Section title="引用" note="CitationBadge：句中的来源编号">
      <Specimen name="CitationBadge" use="悬停或聚焦时显示来源卡片">
        <p className="vr-markdown max-w-md">
          Avibe 默认在本机运行 Agent<CitationBadge citation={citation(1, 'docs.avibe.bot', 'Avibe 文档 · 本地优先')} />，远程访问走加密隧道
          <CitationBadge citation={citation(2, 'avibe.bot', 'Avibe Cloud 远程访问')} />。
        </p>
      </Specimen>
    </Section>
  </Sheet>
);

const citationOpen = () => (
  <div className="p-8">
    <p className="vr-markdown max-w-md">
      Avibe 默认在本机运行 Agent<CitationBadge citation={citation(1, 'docs.avibe.bot', 'Avibe 文档 · 本地优先')} />，远程访问走加密隧道。
    </p>
  </div>
);

const designFrames = frames([
  { name: 'all', title: '徽标 · 变体、品牌胶囊、伙伴与引用', width: 1280, height: 760, status: 'shipped', fixtures: systemFixtures(), render: badgesSheet },
  {
    name: 'citation-card',
    title: '徽标 · 引用来源卡片',
    width: 520,
    height: 220,
    status: 'shipped',
    fixtures: systemFixtures(),
    hover: '[data-citation-index="1"]',
    render: citationOpen,
  },
]);

export default designFrames;
