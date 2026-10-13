// A hosted tool call in the execution-process group (`vibey-hosted-tools.md` §6.4,
// design.pen frames DA8aJ and t2P7VH): the query, how it ran (natively, or by which
// executor), what it cost when known, and the sources or image it produced. A
// native and a proxied call draw the same card from the same record.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AtSign, Check, ChevronRight, CircleSlash, Download, FileImage, Globe, Image as ImageIcon, TriangleAlert, Waypoints, WandSparkles } from 'lucide-react';
import clsx from 'clsx';

import { ChatImage } from '../ui/chat-image';
import { apiKeyVendorPreset } from '../settings/models/apiKeyVendors';
import type { HostedActivity, HostedActivityImage, HostedToolName } from '../../lib/hostedActivity';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generation: ImageIcon,
};

const vendorLabel = (provider: string | undefined): string | null =>
  provider ? apiKeyVendorPreset(provider)?.label ?? provider : null;

// Small per-call figures keep their first significant digits: $0.006, not $0.01.
const formatUsd = (usd: number): string => {
  if (usd >= 0.1) return usd.toFixed(2);
  const digits = usd >= 0.001 ? 3 : 4;
  return usd.toFixed(digits).replace(/0+$/, '').replace(/\.$/, '');
};

const domainOf = (url: string): string => {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
};

const fileName = (path: string): string => path.split(/[\\/]/).pop() || path;

const formatBytes = (bytes: number): string =>
  bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

/** What a call cost, only when it is known; unknown is never shown as zero. */
const useHostedCost = (card: HostedActivity): string | null => {
  const { t } = useTranslation();
  const cost = card.cost;
  if (!cost) return null;
  const figure = cost.basis === 'included'
    ? t('chat.agentActivity.hosted.cost.included')
    : cost.usd === undefined
      ? null
      : t(cost.basis === 'list' ? 'chat.agentActivity.hosted.cost.list' : 'chat.agentActivity.hosted.cost.reported', { usd: formatUsd(cost.usd) });
  if (figure === null) return null;
  return cost.calls > 1 ? t('chat.agentActivity.hosted.cost.calls', { cost: figure, count: cost.calls }) : figure;
};

/** 「原生 · Anthropic」 or 「由 xAI 代理」, and the failure state when it failed. */
export const HostedModeChip: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  if (card.status === 'failed' || card.status === 'incomplete') {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-destructive/40 bg-destructive/10 px-2 py-px text-[11px] font-medium text-destructive-ink">
        <TriangleAlert className="size-3" aria-hidden="true" />
        {t(`chat.agentActivity.hosted.status.${card.status}`)}
      </span>
    );
  }
  if (card.status === 'pending') {
    // Cut off with its turn: it never finished, so it claims no way of having run.
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-gold/40 bg-gold/10 px-2 py-px text-[11px] font-medium text-gold-ink">
        <CircleSlash className="size-3" aria-hidden="true" />
        {t('chat.agentActivity.hosted.status.pending')}
      </span>
    );
  }
  const vendor = vendorLabel(card.provider);
  if (!vendor) return null;
  return card.mode === 'native' ? (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-mint/40 bg-mint-soft px-2 py-px text-[11px] font-medium text-mint-ink">
      <Check className="size-3" aria-hidden="true" />
      {t('chat.agentActivity.hosted.mode.native', { provider: vendor })}
    </span>
  ) : (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan/40 bg-cyan-soft px-2 py-px text-[11px] font-medium text-cyan-ink">
      <Waypoints className="size-3" aria-hidden="true" />
      {t('chat.agentActivity.hosted.mode.proxy', { provider: vendor })}
    </span>
  );
};

/** What the row counts ("1 image · 1536×1024", "2 posts"), when there is anything to count. */
const useCountLabel = (card: HostedActivity): string | null => {
  const { t } = useTranslation();
  if (card.tool === 'image_generation') {
    if (card.images.length === 0) return null;
    const [first] = card.images;
    const count = t('chat.agentActivity.hosted.count.images', { count: card.images.length });
    return first.width && first.height ? `${count} · ${first.width}×${first.height}` : count;
  }
  if (card.sources.length === 0) return null;
  return t(card.tool === 'x_search' ? 'chat.agentActivity.hosted.count.posts' : 'chat.agentActivity.hosted.count.sources', { count: card.sources.length });
};

/** A generated image as the answer and the card show it: the file, how it ran, and the prompt it rendered. */
export const HostedImageFigure: React.FC<{ card: HostedActivity; image: HostedActivityImage }> = ({ card, image }) => {
  const { t } = useTranslation();
  const cost = useHostedCost(card);
  const name = fileName(image.path);
  return (
    <figure className="overflow-hidden rounded-lg border border-border bg-background" data-hosted-image={card.call_id}>
      {image.media_url
        ? <ChatImage src={image.media_url} alt={card.revised_prompt ?? card.subject ?? name} width={image.width} height={image.height} />
        : null}
      <figcaption className="flex flex-col">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-[11px] text-muted">
          <HostedModeChip card={card} />
          {cost && <span>{cost}</span>}
          <span className="ml-auto inline-flex min-w-0 items-center gap-1.5">
            <FileImage className="size-3.5 shrink-0 text-cyan-ink" aria-hidden="true" />
            {image.media_url
              ? <a href={image.media_url} download={name} className="truncate font-medium text-cyan-ink hover:underline" title={name}>{name}</a>
              : <span className="truncate font-medium text-foreground" title={image.path}>{name}</span>}
            {image.bytes !== undefined && <span className="shrink-0">{formatBytes(image.bytes)}</span>}
            {image.media_url && <a href={image.media_url} download={name} aria-label={t('chat.agentActivity.hosted.image.download', { name })} className="shrink-0 text-muted hover:text-foreground"><Download className="size-3.5" aria-hidden="true" /></a>}
          </span>
        </span>
        {card.revised_prompt && (
          <span className="flex flex-col gap-1 border-t border-border px-3 py-2">
            <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-muted">
              <WandSparkles className="size-3" aria-hidden="true" />
              {t('chat.agentActivity.hosted.image.revisedPrompt')}
            </span>
            <span className="text-[12px] leading-relaxed text-foreground/90">{card.revised_prompt}</span>
          </span>
        )}
      </figcaption>
    </figure>
  );
};

const HostedDetail: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  const cost = useHostedCost(card);
  const where = [card.source_name ?? vendorLabel(card.provider), card.model].filter(Boolean).join(' · ');
  const facts = [
    where && t(card.mode === 'proxy' ? 'chat.agentActivity.hosted.ranProxy' : 'chat.agentActivity.hosted.ranNative', { where }),
    card.duration_ms !== undefined && t('chat.agentActivity.hosted.duration', { seconds: Math.max(1, Math.round(card.duration_ms / 1000)) }),
    cost,
  ].filter(Boolean);
  return (
    <div className="mx-1.5 mb-1 mt-0.5 flex flex-col overflow-hidden rounded-md border border-border bg-foreground/[0.03]">
      {facts.length > 0 && (
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5 text-[11px] text-muted">
          {card.mode === 'proxy' ? <Waypoints className="size-3 shrink-0 text-cyan-ink" aria-hidden="true" /> : <Check className="size-3 shrink-0 text-mint-ink" aria-hidden="true" />}
          <span className="min-w-0">{facts.join(' · ')}</span>
        </div>
      )}
      {(card.status === 'failed' || card.status === 'incomplete') && card.error && (
        <p className="px-2.5 py-2 text-[12px] text-destructive-ink">{card.error}</p>
      )}
      {card.extra_queries && card.extra_queries.length > 0 && (
        <p className="border-b border-border px-2.5 py-1.5 text-[11px] text-muted">
          {t('chat.agentActivity.hosted.alsoSearched', { queries: card.extra_queries.join(' · ') })}
        </p>
      )}
      {card.sources.length > 0 && (
        <ol className="flex flex-col">
          {card.sources.map((source, index) => (
            <li key={`${source.url}:${index}`} className="flex gap-2.5 border-b border-border px-2.5 py-2 last:border-b-0">
              <span className="mt-px inline-flex size-[18px] shrink-0 items-center justify-center rounded border border-mint/30 bg-mint/[0.08] text-[10px] font-semibold text-mint-ink">{index + 1}</span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                  <a href={source.url} target="_blank" rel="noopener noreferrer" className="min-w-0 truncate text-[12px] font-medium text-foreground hover:underline" title={source.title ?? source.url}>{source.title || domainOf(source.url)}</a>
                  <span className="shrink-0 text-[11px] text-muted">{[domainOf(source.url), source.page_age].filter(Boolean).join(' · ')}</span>
                </span>
                {source.snippet && <span className="text-[11px] leading-relaxed text-muted">{source.snippet}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
      {card.images.map((image) => (
        <div key={image.path} className="p-2"><HostedImageFigure card={card} image={image} /></div>
      ))}
    </div>
  );
};

export const HostedToolRow: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const Icon = TOOL_ICON[card.tool];
  const cost = useHostedCost(card);
  const count = useCountLabel(card);
  return (
    <div className="flex flex-col" data-hosted-call={card.call_id}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        title={open ? t('chat.agentActivity.hideCall') : t('chat.agentActivity.showCall')}
        className="group/act flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-foreground/[0.04]"
      >
        <Icon className="size-3.5 shrink-0 text-cyan-ink" aria-hidden="true" />
        <span className="shrink-0 font-mono text-[11px] font-medium text-foreground">{card.tool}</span>
        {card.subject
          ? <span className="min-w-0 flex-1 truncate"><span className="rounded-md border border-border bg-foreground/[0.03] px-1.5 py-px text-[12px] text-foreground/90" title={card.subject}>{card.subject}</span></span>
          : <span className="min-w-0 flex-1" />}
        <HostedModeChip card={card} />
        {cost && card.status === 'completed' && <span className="shrink-0 text-[11px] text-muted">{cost}</span>}
        {count && <span className="hidden shrink-0 text-[11px] text-muted sm:inline">{count}</span>}
        <ChevronRight className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-90')} aria-hidden="true" />
      </button>
      {open && <HostedDetail card={card} />}
    </div>
  );
};
