import { memo, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleSlash,
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
import {
  activityDurationParts,
  parseToolName,
  toolIconKind,
  toolSummary,
  type ActivityGroup,
  type ActivityRow,
  type ActivityStatus,
  type ToolIconKind,
} from '../../lib/agentActivity';

// Shared status → icon + tint. ``running`` and ``done`` are mint (success family),
// ``failed`` destructive, ``interrupted`` gold — mirrors the design states A–E.
const STATUS_TINT: Record<ActivityStatus, string> = {
  running: 'text-mint',
  done: 'text-mint',
  failed: 'text-destructive',
  interrupted: 'text-gold',
};

// Static tool-icon table (looked up by kind, never constructed during render).
const TOOL_ICON: Record<ToolIconKind, LucideIcon> = {
  terminal: Terminal,
  edit: Pencil,
  file: FileText,
  web: Globe,
  agent: Bot,
  wrench: Wrench,
};

const stepLabel = (t: (k: string, o?: Record<string, unknown>) => string, count: number): string =>
  t(count === 1 ? 'chat.agentActivity.step' : 'chat.agentActivity.steps', { count });

// ----- One tool-call row: icon + tool name (mono) + one-line summary; click to
// reveal the full stored call text (from agent_events.content_json). -----
const ActivityToolRow: React.FC<{ row: ActivityRow }> = ({ row }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const name = parseToolName(row.text);
  const summary = toolSummary(row.text);
  const Icon = TOOL_ICON[toolIconKind(name)];
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
        <span className="shrink-0 font-mono text-[11px] font-medium text-foreground">{name || '—'}</span>
        {summary ? (
          <span className="min-w-0 flex-1 truncate text-[12px] text-muted">{summary}</span>
        ) : (
          <span className="min-w-0 flex-1" />
        )}
        <ChevronRight
          className={clsx('size-3.5 shrink-0 text-muted transition-transform', open && 'rotate-90')}
          aria-hidden="true"
        />
      </button>
      {open && (
        <pre className="mx-1.5 mb-1 mt-0.5 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-foreground/[0.03] px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted">
          {row.text}
        </pre>
      )}
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

const ActivityRows: React.FC<{ rows: ActivityRow[] }> = ({ rows }) => (
  <div className="flex flex-col gap-0.5">
    {rows.map((row) => (
      <ActivityRowItem key={row.id} row={row} />
    ))}
  </div>
);

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
}> = ({ rows, startedAtMs, expanded, onToggleExpanded }) => {
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
  }, [rows.length, expanded, following]);

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
        {/* Header — click toggles compact ↔ expanded (design interaction rule). */}
        <button
          type="button"
          onClick={onToggleExpanded}
          aria-expanded={expanded}
          className="flex items-center gap-2 border-b border-mint/20 bg-mint/[0.09] px-3 py-2 text-left"
        >
          <Loader2 className="size-3.5 shrink-0 animate-spin text-mint" aria-hidden="true" />
          <span className="text-[12px] font-medium text-mint">{t('chat.agentActivity.running')}</span>
          {rows.length > 0 && (
            <span className="text-[12px] text-muted">· {stepLabel(t, rows.length)}</span>
          )}
          <span className="ml-auto shrink-0 font-mono text-[11px] text-muted">{clock}</span>
          <ChevronDown
            className={clsx('size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')}
            aria-hidden="true"
          />
        </button>
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
              <ActivityRows rows={rows} />
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
            <ActivityRows rows={rows} />
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
// above; click to expand the stored rows (lazy-loaded by the parent). =====
export const ActivityChip: React.FC<{
  group: ActivityGroup;
  expanded: boolean;
  loading: boolean;
  error?: boolean;
  onToggle: () => void;
  onRetry?: () => void;
}> = ({ group, expanded, loading, error, onToggle, onRetry }) => {
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

  return (
    <div className="flex w-full justify-start">
      <div className="flex w-full max-w-[min(92%,860px)] flex-col gap-1">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          title={expanded ? t('chat.agentActivity.collapse') : t('chat.agentActivity.expand')}
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
          <ChevronDown
            className={clsx('size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')}
            aria-hidden="true"
          />
        </button>
        {expanded && (
          <div className="w-full rounded-xl rounded-tl-md border border-border bg-foreground/[0.02] px-1.5 py-1.5">
            {loading ? (
              <div className="flex items-center justify-center py-3 text-muted">
                <Loader2 className="size-4 animate-spin" />
              </div>
            ) : group.rows && group.rows.length > 0 ? (
              <ActivityRows rows={group.rows} />
            ) : error ? (
              // A transient detail-fetch failure — show a retry, not a misleading
              // "no activity", since the summary already counted steps.
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
            )}
          </div>
        )}
      </div>
    </div>
  );
};
