// A hosted tool call in the execution-process group (`vibey-hosted-tools.md` §6.4,
// design.pen frames DA8aJ and t2P7VH): the query, how it ran (natively, by which
// executor, or directly on an image endpoint), what it cost when known, and the
// sources or image it produced. Every mode draws the same card from the same record.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AtSign, Check, ChevronRight, CircleSlash, Download, FileImage, Globe, Image as ImageIcon, TriangleAlert, Type, Waypoints } from 'lucide-react';
import clsx from 'clsx';

import { ChatImage } from '../ui/chat-image';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { apiKeyVendorPreset } from '../settings/models/apiKeyVendors';
import { serverText } from '../settings/models/serverCopy';
import { hostedCompleted, type HostedActivity, type HostedActivityImage, type HostedToolName } from '../../lib/hostedActivity';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generate: ImageIcon,
};

const RAN_KEY: Record<HostedActivity['mode'], 'ranNative' | 'ranProxy' | 'ranDirect'> = {
  native: 'ranNative',
  proxy: 'ranProxy',
  direct: 'ranDirect',
};

/** The catalog label of the vendor that ran a call; none for an id the catalog does not name, such as `custom`. */
const presetLabel = (provider: string | undefined): string | null =>
  provider ? apiKeyVendorPreset(provider)?.label ?? null : null;

/** Who ran a call: its vendor's catalog label, else the Source's own name. A compatible endpoint's
 *  vendor id is `custom`, which names no vendor, so its name is the only thing that says where the
 *  call ran. A search service is named by its Source first, as the user named it. A vendor id with
 *  neither stays as it is, as for a row stored without a Source. The IM line reads the same rule. */
const runnerLabel = (card: HostedActivity): string | null => {
  const vendor = presetLabel(card.provider);
  if (vendor && card.executor_kind !== 'service') return vendor;
  return card.source_name ?? vendor ?? card.provider ?? null;
};

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

/** A source's link, only to a web page. The producer sends only such URLs; rows stored before it did may hold any string. */
const webUrl = (url: string | undefined): string | undefined => {
  if (!url) return undefined;
  try {
    const { protocol } = new URL(url);
    return protocol === 'https:' || protocol === 'http:' ? url : undefined;
  } catch {
    return undefined;
  }
};

const fileName = (path: string): string => path.split(/[\\/]/).pop() || path;

const formatBytes = (bytes: number): string =>
  bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

/**
 * What a call cost, only when it is known; unknown is never shown as zero, and a
 * lower bound (an attempt reported no price) never as a complete figure.
 */
const useHostedCost = (card: HostedActivity): string | null => {
  const { t } = useTranslation();
  const cost = card.cost;
  if (!cost) return null;
  const figure = cost.basis === 'included'
    ? t('chat.agentActivity.hosted.cost.included') as string
    : cost.usd === undefined
      ? null
      : t(cost.usd === 0 || (cost.basis !== 'list' && !cost.estimate) ? 'chat.agentActivity.hosted.cost.reported' : 'chat.agentActivity.hosted.cost.list', { usd: formatUsd(cost.usd) }) as string;
  if (figure === null) return null;
  const bounded = cost.at_least ? t('chat.agentActivity.hosted.cost.atLeast', { cost: figure }) as string : figure;
  return cost.calls > 1 ? t('chat.agentActivity.hosted.cost.calls', { cost: bounded, count: cost.calls }) as string : bounded;
};

/** 「原生 · Anthropic」 or 「由 xAI 代理」 once it completed; until then, its status. */
export const HostedModeChip: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  if (!hostedCompleted(card)) {
    // It did not finish, so it claims no way of having run: cut off with its turn, or failed.
    return card.status === 'pending' ? (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-gold/40 bg-gold/10 px-2 py-px text-[11px] font-medium text-gold-ink">
        <CircleSlash className="size-3" aria-hidden="true" />
        {t('chat.agentActivity.hosted.status.pending')}
      </span>
    ) : (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-destructive/40 bg-destructive/10 px-2 py-px text-[11px] font-medium text-destructive-ink">
        <TriangleAlert className="size-3" aria-hidden="true" />
        {t(`chat.agentActivity.hosted.status.${card.status === 'incomplete' ? 'incomplete' : 'failed'}`)}
      </span>
    );
  }
  const vendor = runnerLabel(card);
  if (card.mode === 'direct') {
    // An image call names the model it picked and where it ran, not a vendor: the model is the choice.
    const where = card.source_name ?? vendor;
    const label = card.model && where
      ? t('chat.agentActivity.hosted.mode.directModel', { model: card.model, source: where })
      : vendor && t('chat.agentActivity.hosted.mode.direct', { provider: vendor });
    if (!label) return null;
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan/40 bg-cyan-soft px-2 py-px text-[11px] font-medium text-cyan-ink">
        <ImageIcon className="size-3" aria-hidden="true" />
        {label}
      </span>
    );
  }
  if (!vendor) return null;
  if (card.executor_kind === 'service') {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan/40 bg-cyan-soft px-2 py-px text-[11px] font-medium text-cyan-ink">
        <Waypoints className="size-3" aria-hidden="true" />
        {t('chat.agentActivity.hosted.mode.service', { provider: vendor })}
      </span>
    );
  }
  return card.mode === 'native' ? (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-mint/40 bg-mint-soft px-2 py-px text-[11px] font-medium text-mint-ink">
      <Check className="size-3" aria-hidden="true" />
      {t('chat.agentActivity.hosted.mode.native', { provider: vendor })}
    </span>
  ) : (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan/40 bg-cyan-soft px-2 py-px text-[11px] font-medium text-cyan-ink">
      <Waypoints className="size-3" aria-hidden="true" />
      {card.model
        ? t('chat.agentActivity.hosted.mode.proxyModel', { provider: vendor, model: card.model })
        : t('chat.agentActivity.hosted.mode.proxy', { provider: vendor })}
    </span>
  );
};

/** What the row counts ("1 image · 1536×1024", "2 posts"), when there is anything to count. */
const useCountLabel = (card: HostedActivity): string | null => {
  const { t } = useTranslation();
  if (!hostedCompleted(card)) return null;
  if (card.tool === 'image_generate') {
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
          {card.mode === 'direct' && card.model
            ? (
                <>
                  {/* The image as made: the model and its size, then where it ran. */}
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan/40 bg-cyan-soft px-2 py-px font-medium text-cyan-ink">
                    <ImageIcon className="size-3" aria-hidden="true" />
                    {image.width && image.height ? `${card.model} · ${image.width}×${image.height}` : card.model}
                  </span>
                  {(card.source_name ?? runnerLabel(card)) && <span>{card.source_name ?? runnerLabel(card)}</span>}
                </>
              )
            : <HostedModeChip card={card} />}
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
              <Type className="size-3" aria-hidden="true" />
              {t('chat.agentActivity.hosted.image.prompt')}
            </span>
            <span className="text-[12px] leading-relaxed text-foreground/90">{card.revised_prompt}</span>
          </span>
        )}
      </figcaption>
    </figure>
  );
};

/**
 * What a search chain's Sources did, in the words the producer chose for them (`core/hosted_activity.py`): for a
 * completed call, the Sources it moved past before the one that answered; for one that did not complete, every
 * Source it reached, each with what it needs now, else what happened to it. Both stay in view, collapsed or
 * expanded, as the IM line carries them.
 */
const HostedAttemptNotes: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  // A key the producer chose; one this client does not know yet reads as a plain failure.
  const say = (key: string | undefined) => serverText(t, key && `chat.agentActivity.hosted.${key}`, 'chat.agentActivity.hosted.reason.failed');
  if (hostedCompleted(card)) {
    const note = card.switch;
    if (!note) return null;
    const failed = `${note.sources.join(t('chat.agentActivity.hosted.listSeparator'))} ${say(note.reason_key)}`;
    return (
      <p className="mx-1.5 mb-1 mt-0.5 flex items-center gap-1.5 rounded-md border border-gold/40 bg-gold/10 px-2.5 py-1.5 text-[11px] text-foreground" data-hosted-switch={card.call_id}>
        <TriangleAlert className="size-3 shrink-0 text-gold-ink" aria-hidden="true" />
        {t('chat.agentActivity.hosted.switch', { failed, provider: note.to })}
      </p>
    );
  }
  const items = (card.attempts ?? [])
    .filter((attempt) => attempt.remedy_key || attempt.reason_key)
    .map((attempt) => `${attempt.label} ${say(attempt.remedy_key ?? attempt.reason_key)}`);
  if (items.length === 0) return null;
  return (
    <p className="mx-1.5 mb-1 mt-0.5 rounded-md border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-[11px] text-destructive-ink" data-hosted-attempts={card.call_id}>
      {items.join(t('chat.agentActivity.hosted.itemSeparator'))}
    </p>
  );
};

const HostedDetail: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  const cost = useHostedCost(card);
  const completed = hostedCompleted(card);
  const where = completed ? [card.source_name ?? runnerLabel(card), card.model].filter(Boolean).join(' · ') : '';
  const ran = card.executor_kind === 'service' ? 'ranService' : RAN_KEY[card.mode];
  const facts = [
    where && t(`chat.agentActivity.hosted.${ran}`, { where }),
    completed && card.duration_ms !== undefined && t('chat.agentActivity.hosted.duration', { seconds: Math.max(1, Math.round(card.duration_ms / 1000)) }),
    cost,
  ].filter(Boolean);
  return (
    <div className="mx-1.5 mb-1 mt-0.5 flex flex-col overflow-hidden rounded-md border border-border bg-foreground/[0.03]">
      {facts.length > 0 && (
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5 text-[11px] text-muted">
          {completed && (card.mode === 'native'
            ? <Check className="size-3 shrink-0 text-mint-ink" aria-hidden="true" />
            : card.mode === 'direct'
              ? <ImageIcon className="size-3 shrink-0 text-cyan-ink" aria-hidden="true" />
              : <Waypoints className="size-3 shrink-0 text-cyan-ink" aria-hidden="true" />)}
          <span className="min-w-0">{facts.join(' · ')}</span>
        </div>
      )}
      {(card.status === 'failed' || card.status === 'incomplete') && card.error && (
        <p className="px-2.5 py-2 text-[12px] text-destructive-ink">
          {/* Avibe's own reasons in the reader's language; a provider's, or a code this
              client does not know yet, as it was written. */}
          {(card.error_code && serverText(t, `chat.agentActivity.hosted.errorCode.${card.error_code}`)) || card.error}
        </p>
      )}
      {card.extra_queries && card.extra_queries.length > 0 && (
        <p className="border-b border-border px-2.5 py-1.5 text-[11px] text-muted">
          {t('chat.agentActivity.hosted.alsoSearched', { queries: card.extra_queries.join(' · ') })}
        </p>
      )}
      {completed && card.sources.length > 0 && (
        <ol className="flex flex-col">
          {card.sources.map((source, index) => {
            const href = webUrl(source.url);
            const name = source.title || (href ? domainOf(href) : source.url);
            return (
              <li key={`${source.url ?? source.title}:${index}`} className="flex gap-2.5 border-b border-border px-2.5 py-2 last:border-b-0">
                <span className="mt-px inline-flex size-[18px] shrink-0 items-center justify-center rounded border border-mint/30 bg-mint/[0.08] text-[10px] font-semibold text-mint-ink">{index + 1}</span>
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                    {href
                      ? <a href={href} target="_blank" rel="noopener noreferrer" className="min-w-0 truncate text-[12px] font-medium text-foreground hover:underline" title={source.title ?? href}>{name}</a>
                      : <span className="min-w-0 truncate text-[12px] font-medium text-foreground" title={name}>{name}</span>}
                    <span className="shrink-0 text-[11px] text-muted">{[href && domainOf(href), source.page_age].filter(Boolean).join(' · ')}</span>
                  </span>
                  {source.snippet && <span className="text-[11px] leading-relaxed text-muted">{source.snippet}</span>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {completed && card.images.map((image) => (
        <div key={image.path} className="p-2"><HostedImageFigure card={card} image={image} /></div>
      ))}
    </div>
  );
};

/**
 * Why a search ran by proxy: the turn's own Source refused it natively. The note
 * stays in view, collapsed or expanded, and says the refusal lasts until it is
 * cleared: where to clear it for a reader who can, and who can for anyone else.
 */
const FallbackNote: React.FC<{ card: HostedActivity }> = ({ card }) => {
  const { t } = useTranslation();
  // Only an instance manager reaches Models › Tools; anyone else is told who can clear it.
  const { capabilities } = useInstanceAuthorization();
  const from = card.fallback_from;
  const vendor = runnerLabel(card);
  if (!from || !hostedCompleted(card) || !vendor) return null;
  const refusedBy = from.source_name ?? from.source_id;
  const toolInline = t(`settings.models.tools.tool.${card.tool}.inline`);
  // The Source in parentheses only beside a vendor's label: a Source named in its vendor's place is not repeated.
  const besideVendor = presetLabel(card.provider) ? card.source_name : undefined;
  return (
    <p className="mx-1.5 mb-1 mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-md border border-gold/40 bg-gold/10 px-2.5 py-1.5 text-[11px]" data-hosted-fallback={card.call_id}>
      <span className="inline-flex min-w-0 items-center gap-1.5 text-foreground">
        <TriangleAlert className="size-3 shrink-0 text-gold-ink" aria-hidden="true" />
        {besideVendor
          ? t('chat.agentActivity.hosted.fallback.ranOn', { from: refusedBy, toolInline, provider: vendor, source: besideVendor })
          : t('chat.agentActivity.hosted.fallback.ran', { from: refusedBy, toolInline, provider: vendor })}
      </span>
      <span className="ml-auto text-muted">{t(capabilities.can_manage_instance ? 'chat.agentActivity.hosted.fallback.kept' : 'chat.agentActivity.hosted.fallback.keptManager')}</span>
    </p>
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
        {cost && <span className="shrink-0 text-[11px] text-muted">{cost}</span>}
        {count && <span className="hidden shrink-0 text-[11px] text-muted sm:inline">{count}</span>}
        <ChevronRight className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-90')} aria-hidden="true" />
      </button>
      <HostedAttemptNotes card={card} />
      <FallbackNote card={card} />
      {open && <HostedDetail card={card} />}
    </div>
  );
};
