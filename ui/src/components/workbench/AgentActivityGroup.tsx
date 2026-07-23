import { lazy, memo, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  Bot,
  Braces,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  Copy,
  Eye,
  EyeOff,
  FileText,
  Globe,
  Loader2,
  Pencil,
  Sparkles,
  Terminal,
  Wrench,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { Markdown } from '../ui/markdown';
import { Dialog, DialogContent } from '../ui/dialog';
import { copyTextToClipboard } from '../../lib/utils';
import {
  activityDurationParts,
  filterActivityRows,
  genericChips,
  parseToolCall,
  parseToolName,
  toolIconKind,
  toolParams,
  toolRecipe,
  toolSummary,
  type ActivityGroup,
  type ActivityRow,
  type ActivityStatus,
  type FileOp,
  type ParsedToolCall,
  type ToolIconKind,
} from '../../lib/agentActivity';

// The full-JSON viewer is the same lazy, syntax-highlighted tree FileViewer uses —
// loaded only when a JSON dialog opens (zero new deps; see file-preview.tsx).
const PreviewJson = lazy(() => import('../ui/preview-json'));

// Shared status → icon + tint. ``running`` and ``done`` are mint (success family),
// ``failed`` destructive, ``interrupted`` gold — mirrors the design states A–E.
const STATUS_TINT: Record<ActivityStatus, string> = {
  running: 'text-mint',
  done: 'text-mint',
  failed: 'text-destructive',
  interrupted: 'text-gold',
};

// Status → chip/header border+background tint (shared by the collapsed chip and the
// expanded panel header so a done panel reads mint, failed destructive, etc.).
const statusChipClasses = (status: ActivityStatus): string =>
  status === 'failed'
    ? 'border-destructive/30 bg-destructive/[0.06]'
    : status === 'interrupted'
      ? 'border-gold/30 bg-gold/[0.07]'
      : 'border-mint/25 bg-mint/[0.07]';

// Static tool-icon table (looked up by kind, never constructed during render).
const TOOL_ICON: Record<ToolIconKind, LucideIcon> = {
  terminal: Terminal,
  edit: Pencil,
  file: FileText,
  web: Globe,
  agent: Bot,
  wrench: Wrench,
};

// File operation → badge glyph + i18n key (the mint-soft "+ 新增" style chip in A).
const FILE_OP_META: Record<FileOp, { glyph: string; i18nKey: string; className: string }> = {
  create: { glyph: '+', i18nKey: 'chat.agentActivity.opCreate', className: 'border-mint/30 bg-mint/[0.08] text-mint' },
  modify: { glyph: '~', i18nKey: 'chat.agentActivity.opModify', className: 'border-cyan/30 bg-cyan/[0.08] text-cyan' },
  delete: { glyph: '−', i18nKey: 'chat.agentActivity.opDelete', className: 'border-gold/30 bg-gold/[0.08] text-gold' },
};

const stepLabel = (t: (k: string, o?: Record<string, unknown>) => string, count: number): string =>
  t(count === 1 ? 'chat.agentActivity.step' : 'chat.agentActivity.steps', { count });

// ----- B: tool-row visibility pill (eye / eye-off + "Tools"). Global, config-backed
// (``config.ui.show_tool_calls``); the label collapses to icon-only on narrow widths
// (tooltip keeps it). A plain button — never nested inside another button. -----
const ToolsEyePill: React.FC<{ shown: boolean; onToggle: () => void }> = ({ shown, onToggle }) => {
  const { t } = useTranslation();
  const label = t('chat.agentActivity.tools');
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={shown}
      title={shown ? t('chat.agentActivity.hideTools') : t('chat.agentActivity.showTools')}
      className={clsx(
        'inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors',
        shown
          ? 'border-border bg-foreground/[0.04] text-foreground/80 hover:bg-foreground/[0.08]'
          : 'border-border bg-transparent text-muted hover:bg-foreground/[0.05]',
      )}
    >
      {shown ? <Eye className="size-3.5" aria-hidden="true" /> : <EyeOff className="size-3.5" aria-hidden="true" />}
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
};

// ----- D: full-JSON dialog. Reuses the shared Dialog + the lazy JSON viewer; a parse
// failure shows the raw text (still copyable). ESC/backdrop close via Radix. -----
const ToolJsonDialog: React.FC<{ open: boolean; onClose: () => void; parsed: ParsedToolCall }> = ({
  open,
  onClose,
  parsed,
}) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copyText = parsed.args ? JSON.stringify(parsed.args, null, 2) : parsed.raw;
  const copy = async () => {
    if (await copyTextToClipboard(copyText)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onClose() : undefined)}>
      <DialogContent className="max-w-2xl gap-0 overflow-hidden p-0">
        {/* ``DialogContent`` renders its own absolute close X at right-4 top-4;
            reserve right padding so the Copy action never sits under it. */}
        <div className="flex items-center gap-2 border-b border-border py-2.5 pl-4 pr-12">
          <Terminal className="size-3.5 shrink-0 text-muted" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-foreground">
            {parsed.name || '—'}
          </span>
          <button
            type="button"
            onClick={copy}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground/80 transition-colors hover:bg-foreground/[0.06]"
          >
            {copied ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5" />}
            {copied ? t('chat.agentActivity.copied') : t('chat.agentActivity.copy')}
          </button>
        </div>
        <div className="max-h-[60vh] overflow-auto px-2 py-2">
          {parsed.args ? (
            <Suspense
              fallback={<div className="p-3 text-[12px] text-muted">{t('chat.agentActivity.jsonLoading')}</div>}
            >
              <PreviewJson value={parsed.args} />
            </Suspense>
          ) : (
            <pre className="whitespace-pre-wrap break-words px-2 font-mono text-[11px] leading-relaxed text-muted">
              {parsed.raw}
            </pre>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};

// ----- A: one-line tool summary. Tier 1 known-tool recipe (command / path / file-op /
// query / description) → tier 2 generic kv chips → tier 3 raw text (today). -----
const ToolSummary: React.FC<{ parsed: ParsedToolCall }> = ({ parsed }) => {
  const { t } = useTranslation();
  const recipe = parsed.args ? toolRecipe(parsed.name, parsed.args) : null;

  if (recipe) {
    switch (recipe.kind) {
      case 'command':
        return (
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground/80">
            <span className="text-muted">$ </span>
            {recipe.command}
          </span>
        );
      case 'read':
        return (
          <span className="min-w-0 flex-1 truncate text-[12px]">
            {recipe.dir && <span className="text-muted">{recipe.dir}</span>}
            <span className="font-medium text-foreground">{recipe.base}</span>
          </span>
        );
      case 'fileop': {
        const op = FILE_OP_META[recipe.op];
        return (
          <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-[12px]">
            <span className="min-w-0 truncate">
              {recipe.dir && <span className="text-muted">{recipe.dir}</span>}
              <span className="font-medium text-foreground">{recipe.base}</span>
            </span>
            <span
              className={clsx('shrink-0 rounded-full border px-1.5 py-px text-[10px] font-medium', op.className)}
            >
              {op.glyph} {t(op.i18nKey)}
            </span>
          </span>
        );
      }
      case 'query':
        return (
          <span className="min-w-0 flex-1 truncate text-[12px] text-foreground/80">「{recipe.text}」</span>
        );
      case 'text':
        return <span className="min-w-0 flex-1 truncate text-[12px] text-foreground/80">{recipe.text}</span>;
    }
  }

  if (parsed.args) {
    // Tier 2: unknown tool, JSON parsed → generic kv chips + overflow.
    const { chips, overflow } = genericChips(parsed.args);
    if (chips.length === 0 && overflow === 0) return <span className="min-w-0 flex-1" />;
    return (
      <span className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
        {chips.map((chip) => (
          <span
            key={chip.key}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-foreground/[0.03] px-1.5 py-px text-[11px]"
          >
            <span className="font-mono text-muted">{chip.key}</span>
            <span className="max-w-[16ch] truncate text-foreground/80">{chip.value}</span>
          </span>
        ))}
        {overflow > 0 && <span className="shrink-0 text-[11px] text-muted">+{overflow}</span>}
      </span>
    );
  }

  // Tier 3: parse failed / no params / oversized → today's raw one-liner.
  const summary = toolSummary(parsed.raw);
  return summary ? (
    <span className="min-w-0 flex-1 truncate text-[12px] text-muted">{summary}</span>
  ) : (
    <span className="min-w-0 flex-1" />
  );
};

// ----- D: expanded inline detail. Same parse as A → a kv table (long/multiline values
// as wrapping code blocks; ``timeout`` humanized); parse failure → raw text. A small
// "{ } JSON" button opens the full-JSON dialog. -----
const ToolDetail: React.FC<{ parsed: ParsedToolCall }> = ({ parsed }) => {
  const { t } = useTranslation();
  const [jsonOpen, setJsonOpen] = useState(false);
  const params = parsed.args ? toolParams(parsed.args) : null;
  return (
    <div className="mx-1.5 mb-1 mt-0.5 rounded-md border border-border bg-foreground/[0.03]">
      <div className="flex items-center gap-2 border-b border-border px-2.5 py-1.5">
        <span className="flex-1 text-[11px] font-medium uppercase tracking-wide text-muted">
          {t('chat.agentActivity.params')}
        </span>
        <button
          type="button"
          onClick={() => setJsonOpen(true)}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] font-medium text-foreground/80 transition-colors hover:bg-foreground/[0.06]"
        >
          <Braces className="size-3" aria-hidden="true" />
          {t('chat.agentActivity.viewJson')}
        </button>
      </div>
      {params && params.length > 0 ? (
        <div className="flex flex-col gap-1 px-2.5 py-2">
          {params.map((p) => (
            <div key={p.key} className={clsx('gap-2 text-[11px]', p.block ? 'flex flex-col' : 'flex items-baseline')}>
              <span className="shrink-0 font-mono text-muted">{p.key}</span>
              {p.block ? (
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-foreground/[0.04] px-2 py-1 font-mono text-[11px] leading-relaxed text-foreground/90">
                  {p.value}
                </pre>
              ) : (
                <span className="min-w-0 flex-1 break-words text-foreground/90">{p.value}</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        // Tier 3 (or empty-args): the raw stored call text, unchanged.
        <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted">
          {parsed.raw}
        </pre>
      )}
      <ToolJsonDialog open={jsonOpen} onClose={() => setJsonOpen(false)} parsed={parsed} />
    </div>
  );
};

// ----- One tool-call row: icon + tool name (mono) + A summary; click to reveal the D
// detail. The whole parse is wrapped so any unexpected shape degrades to tier 3, never
// blank. -----
const ActivityToolRow: React.FC<{ row: ActivityRow }> = ({ row }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const parsed = useMemo<ParsedToolCall>(() => {
    try {
      return parseToolCall(row.text);
    } catch {
      return { name: parseToolName(row.text), args: null, raw: row.text || '' };
    }
  }, [row.text]);
  const Icon = TOOL_ICON[toolIconKind(parsed.name)];
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? t('chat.agentActivity.hideCall') : t('chat.agentActivity.showCall')}
        className="group/act flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-foreground/[0.04]"
      >
        <Icon className="size-3.5 shrink-0 text-muted" aria-hidden="true" />
        <span className="shrink-0 font-mono text-[11px] font-medium text-foreground">{parsed.name || '—'}</span>
        <ToolSummary parsed={parsed} />
        <ChevronRight
          className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-90')}
          aria-hidden="true"
        />
      </button>
      {open && <ToolDetail parsed={parsed} />}
    </div>
  );
};

// ----- One interim assistant row: sparkles + full text (owner decision), the
// existing Markdown renderer in a compact style. -----
const ActivityAssistantRow: React.FC<{ row: ActivityRow }> = ({ row }) => (
  <div className="flex items-start gap-2 px-1.5 py-1">
    <Sparkles className="mt-0.5 size-3.5 shrink-0 text-mint" aria-hidden="true" />
    <div className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground/90 [&_p]:my-0.5 [&_pre]:max-w-full [&_pre]:overflow-x-auto">
      {row.text ? (
        <Markdown content={row.text} className="vr-markdown--inherit-size" />
      ) : (
        <span className="text-muted">—</span>
      )}
    </div>
  </div>
);

const ActivityRowItem = memo(function ActivityRowItem({ row }: { row: ActivityRow }) {
  return row.kind === 'tool_call' ? <ActivityToolRow row={row} /> : <ActivityAssistantRow row={row} />;
});

// ``variant`` picks the all-filtered placeholder: the compact LIVE card shows a single
// "hidden · running" line; expanded panels show a "N hidden" count row (and append that
// count as a trailing line when narration rows are also present).
type RowsVariant = 'compact-live' | 'panel';

const ActivityRows: React.FC<{ rows: ActivityRow[]; showToolCalls: boolean; variant: RowsVariant }> = ({
  rows,
  showToolCalls,
  variant,
}) => {
  const { t } = useTranslation();
  const { visible, hiddenTools } = filterActivityRows(rows, showToolCalls);

  if (visible.length === 0) {
    // Everything filtered (all rows were tool calls, no narration).
    if (variant === 'compact-live') {
      return <div className="px-1.5 py-1 text-[12px] italic text-muted">{t('chat.agentActivity.toolsHiddenLive')}</div>;
    }
    return (
      <div className="px-1.5 py-1 text-[12px] italic text-muted">
        {t('chat.agentActivity.toolsHidden', { count: hiddenTools })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-0.5">
      {visible.map((row) => (
        <ActivityRowItem key={row.id} row={row} />
      ))}
      {variant === 'panel' && hiddenTools > 0 && (
        <div className="px-1.5 pt-0.5 text-[11px] italic text-muted">
          {t('chat.agentActivity.toolsHidden', { count: hiddenTools })}
        </div>
      )}
    </div>
  );
};

// ===== Running card (states A/B): compact fixed-height viewport that never grows,
// or an expanded ~40vh internal scroller that auto-follows the tail. =====
// Compact running-card viewport cap ≈ 3 rows. The body height is min(content, cap):
// it grows downward from a single row (no reserved blank space) and only becomes a
// constant-height, bottom-following, top-fading viewport once content reaches the cap.
const COMPACT_CAP_PX = 110;

export const ActivityCard: React.FC<{
  rows: ActivityRow[];
  startedAtMs: number | null;
  expanded: boolean;
  onToggleExpanded: () => void;
  showToolCalls: boolean;
  onToggleTools: () => void;
}> = ({ rows, startedAtMs, expanded, onToggleExpanded, showToolCalls, onToggleTools }) => {
  const { t } = useTranslation();
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Fallback start if the turn's start time is unknown (defensive — the card only
  // shows once a live row has set it): the card's mount time.
  const [mountedAt] = useState(() => Date.now());
  const scrollRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  // Compact body reached its cap → clamp to the cap + show the top "older rows
  // fading up" gradient. Below the cap the body is exactly content-tall (no fade,
  // no blank space).
  const compactBodyRef = useRef<HTMLDivElement>(null);
  const [compactAtCap, setCompactAtCap] = useState(false);

  // Tick the elapsed clock once a second while mounted (the card only mounts
  // while a live turn is running).
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Auto-follow the tail in the expanded scroller unless the reader scrolled up.
  useEffect(() => {
    if (expanded && following && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [rows.length, expanded, following, showToolCalls]);

  // Measure whether the compact body has hit the cap (content clamped) — drives the
  // top fade so it only appears once older rows are actually clipped. A ResizeObserver
  // ties the gate to ACTUAL layout, so it also tracks resizes that don't change
  // ``rows.length``: a tool row expanding its stored call text, a late-loading image,
  // or a width change reflowing an assistant markdown row.
  useLayoutEffect(() => {
    if (expanded) return;
    const el = compactBodyRef.current;
    if (!el) return;
    const measure = () => setCompactAtCap(el.offsetHeight >= COMPACT_CAP_PX);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [expanded]);

  const elapsedMs = Math.max(0, nowMs - (startedAtMs ?? mountedAt));
  const mm = Math.floor(elapsedMs / 60000);
  const ss = Math.floor((elapsedMs % 60000) / 1000);
  const clock = `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;

  return (
    <div className="flex w-full justify-start">
      <div className="flex w-full max-w-[min(92%,860px)] flex-col overflow-hidden rounded-2xl rounded-tl-md border border-mint/25 bg-background">
        {/* Header — the label/clock region toggles compact ↔ expanded; the eye pill
            (B) is a sibling button (never nested) that flips tool-row visibility. */}
        <div className="flex items-center gap-2 border-b border-mint/20 bg-mint/[0.09] px-3 py-2">
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-expanded={expanded}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
          >
            <Loader2 className="size-3.5 shrink-0 animate-spin text-mint" aria-hidden="true" />
            <span className="text-[12px] font-medium text-mint">{t('chat.agentActivity.running')}</span>
            {rows.length > 0 && <span className="text-[12px] text-muted">· {stepLabel(t, rows.length)}</span>}
            <span className="ml-auto shrink-0 font-mono text-[11px] text-muted">{clock}</span>
          </button>
          <ToolsEyePill shown={showToolCalls} onToggle={onToggleTools} />
          <button type="button" onClick={onToggleExpanded} aria-label={t('chat.agentActivity.collapse')}>
            <ChevronDown
              className={clsx('size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')}
              aria-hidden="true"
            />
          </button>
        </div>
        {expanded ? (
          <div className="relative">
            <div
              ref={scrollRef}
              onScroll={() => {
                const el = scrollRef.current;
                if (!el) return;
                setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
              }}
              className="max-h-[40vh] overflow-y-auto px-1.5 py-1.5 [overflow-anchor:none]"
            >
              <ActivityRows rows={rows} showToolCalls={showToolCalls} variant="panel" />
            </div>
            {!following && (
              <button
                type="button"
                onClick={() => {
                  const el = scrollRef.current;
                  if (el) el.scrollTop = el.scrollHeight;
                  setFollowing(true);
                }}
                className="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full border border-mint/40 bg-background/90 px-2.5 py-1 text-[11px] font-medium text-mint shadow-sm backdrop-blur"
              >
                {t('chat.agentActivity.jumpToLatest')}
              </button>
            )}
          </div>
        ) : (
          // Compact: body height = min(content, cap). Below the cap it is exactly
          // content-tall and grows downward as rows arrive (natural, like any new
          // message at the transcript tail) — no reserved blank space. At the cap it
          // clamps to a constant viewport with the newest rows pinned to the bottom
          // (justify-end) and older rows clipped + faded up. ``overflow-hidden`` clips
          // the top overflow; the tail auto-follow lives in the Transcript scroller.
          <div
            ref={compactBodyRef}
            className="relative flex flex-col justify-end overflow-hidden px-1.5 py-1.5"
            style={{ maxHeight: COMPACT_CAP_PX }}
          >
            <ActivityRows rows={rows} showToolCalls={showToolCalls} variant="compact-live" />
            {compactAtCap && (
              <div className="pointer-events-none absolute inset-x-0 top-0 h-8 bg-gradient-to-b from-background to-transparent" />
            )}
          </div>
        )}
      </div>
    </div>
  );
};

// ===== Collapsed chip (states C/D/E): a one-line summary that hugs the reply from
// above; click to expand into a full panel (header + scrollable rows, capped ~60vh). =====
export const ActivityChip: React.FC<{
  group: ActivityGroup;
  expanded: boolean;
  loading: boolean;
  error?: boolean;
  onToggle: () => void;
  onRetry?: () => void;
  showToolCalls: boolean;
  onToggleTools: () => void;
}> = ({ group, expanded, loading, error, onToggle, onRetry, showToolCalls, onToggleTools }) => {
  const { t } = useTranslation();
  const StatusIcon =
    group.status === 'failed' ? AlertTriangle : group.status === 'interrupted' ? CircleSlash : CheckCircle2;

  let label: string;
  if (group.status === 'failed') {
    label = t('chat.agentActivity.failed', { count: group.steps });
  } else if (group.status === 'interrupted') {
    label = t('chat.agentActivity.interrupted', { count: group.steps });
  } else {
    const parts = activityDurationParts(group.durationMs);
    const duration = parts
      ? parts.minutes > 0
        ? t('chat.agentActivity.durationMin', { minutes: parts.minutes, seconds: parts.seconds })
        : t('chat.agentActivity.durationSec', { seconds: parts.seconds })
      : '';
    label = `${t('chat.agentActivity.label')} · ${stepLabel(t, group.steps)}${duration ? ` · ${duration}` : ''}`;
  }

  const body = loading ? (
    <div className="flex items-center justify-center py-3 text-muted">
      <Loader2 className="size-4 animate-spin" />
    </div>
  ) : group.rows && group.rows.length > 0 ? (
    <ActivityRows rows={group.rows} showToolCalls={showToolCalls} variant="panel" />
  ) : error ? (
    // A transient detail-fetch failure — show a retry, not a misleading "no
    // activity", since the summary already counted steps.
    <div className="flex items-center justify-between gap-2 px-1.5 py-2 text-[12px] text-muted">
      <span>{t('chat.agentActivity.loadFailed')}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-md border border-border px-2 py-0.5 text-[11px] font-medium text-cyan transition-colors hover:bg-surface-2"
        >
          {t('chat.agentActivity.retry')}
        </button>
      )}
    </div>
  ) : (
    <div className="px-1.5 py-2 text-[12px] text-muted">{t('chat.agentActivity.empty')}</div>
  );

  return (
    <div className="flex w-full justify-start">
      <div className="flex w-full max-w-[min(92%,860px)] flex-col gap-1">
        {!expanded ? (
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={false}
            title={t('chat.agentActivity.expand')}
            className={clsx(
              'inline-flex w-fit max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors',
              group.status === 'failed'
                ? 'border-destructive/30 bg-destructive/[0.06] hover:bg-destructive/[0.1]'
                : group.status === 'interrupted'
                  ? 'border-gold/30 bg-gold/[0.07] hover:bg-gold/[0.12]'
                  : 'border-mint/25 bg-mint/[0.07] hover:bg-mint/[0.12]',
            )}
          >
            <StatusIcon className={clsx('size-3.5 shrink-0', STATUS_TINT[group.status])} aria-hidden="true" />
            <span className="min-w-0 truncate font-medium text-foreground/90">{label}</span>
            <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden="true" />
          </button>
        ) : (
          // Expanded: a full panel card. Header bar carries the status/label (toggles
          // collapse), the B eye pill, and a collapse chevron; body is the ~60vh
          // capped, top-anchored scroller (C).
          <div className="w-full overflow-hidden rounded-xl rounded-tl-md border border-border bg-foreground/[0.02]">
            <div className={clsx('flex items-center gap-2 border-b px-3 py-2', statusChipClasses(group.status))}>
              <button
                type="button"
                onClick={onToggle}
                aria-expanded
                title={t('chat.agentActivity.collapse')}
                className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
              >
                <StatusIcon className={clsx('size-3.5 shrink-0', STATUS_TINT[group.status])} aria-hidden="true" />
                <span className="min-w-0 truncate text-[12px] font-medium text-foreground/90">{label}</span>
              </button>
              <ToolsEyePill shown={showToolCalls} onToggle={onToggleTools} />
              <button type="button" onClick={onToggle} aria-label={t('chat.agentActivity.collapse')}>
                <ChevronDown className="size-3.5 shrink-0 rotate-180 text-muted" aria-hidden="true" />
              </button>
            </div>
            <div className="max-h-[60vh] overflow-y-auto px-1.5 py-1.5">{body}</div>
          </div>
        )}
      </div>
    </div>
  );
};
