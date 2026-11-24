// Small pieces the Devices views share.
import type { ReactNode } from 'react';
import { ArrowLeft, ChevronRight, LoaderCircle, Route, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { optimizeNote } from './texts';
import type { OptimizeState } from './types';

/** Back to the Devices page, as other Settings sub-pages draw it; shown at every width. */
export function BackToDevices({ onBack }: { onBack?: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onBack}
      className="inline-flex w-fit items-center gap-1.5 font-mono text-[11px] text-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft className="size-3" />
      {t('devices.backToDevices')}
    </button>
  );
}

export function IconTile({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'on' }) {
  return (
    <span className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${tone === 'on' ? 'bg-mint-soft text-mint-ink' : 'bg-field text-muted'}`}>
      {children}
    </span>
  );
}

/** One line that says something did not happen, or needs attention. `strong`: it must be seen. */
export function Notice({ tone = 'warning', children, action }: { tone?: 'warning' | 'danger' | 'plain'; children: ReactNode; action?: ReactNode }) {
  const tones = {
    warning: 'border-gold/40 bg-gold/10',
    danger: 'border-destructive/40 bg-destructive/10',
    plain: 'border-edge bg-surface',
  } as const;
  return (
    <div role={tone === 'plain' ? 'status' : 'alert'} className={`flex flex-col gap-3 rounded-xl border px-5 py-3 md:flex-row md:items-center ${tones[tone]}`}>
      {tone !== 'plain' && <TriangleAlert className={`size-4 shrink-0 max-md:hidden ${tone === 'danger' ? 'text-destructive-ink' : 'text-gold-ink'}`} />}
      <div className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground">{children}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** A question asked before something is lost. */
export function Confirm({ title, body, action, busy, onConfirm, onCancel, children, destructive = true }: {
  title: string; body?: string; action: string; busy?: boolean; onConfirm?: () => void; onCancel?: () => void;
  children?: ReactNode; destructive?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel?.(); }}>
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle className="text-balance">{title}</DialogTitle>
          {body && <DialogDescription><KeptTail text={body} /></DialogDescription>}
        </DialogHeader>
        {children}
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button variant={destructive ? 'destructive' : 'default'} disabled={busy} onClick={onConfirm}>{action}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** A row that does or opens something, with one line under its name when there is something to say. */
export function ActionRow({ icon: Icon, label, note, failed, busy, disabled, onClick }: {
  icon: typeof Route; label: string; note?: string; failed?: boolean; busy?: boolean; disabled?: boolean; onClick?: () => void;
}) {
  return (
    <button
      type="button"
      disabled={busy || disabled}
      onClick={onClick}
      className="flex w-full items-center gap-3 border-b border-border px-5 py-3 text-left text-[13px] text-foreground transition-colors last:border-b-0 hover:bg-field/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:opacity-60"
    >
      <Icon className="size-4 shrink-0 text-muted" />
      <span className="min-w-0 flex-1">
        <span className="block">{label}</span>
        {note && <span role="status" className={`block text-[11px] leading-relaxed ${failed ? 'text-gold-ink' : 'text-muted'}`}>{note}</span>}
      </span>
      {busy ? <LoaderCircle className="size-4 text-muted motion-safe:animate-spin" /> : <ChevronRight className="size-4 text-muted" />}
    </button>
  );
}

/** 「优化线路」, the same row wherever it is offered. */
export function OptimizeRow({ optimize, onOptimize }: { optimize?: OptimizeState; onOptimize?: () => void }) {
  const { t } = useTranslation();
  return (
    <ActionRow
      icon={Route}
      label={t(optimize?.busy ? 'remoteAccess.optimizingRoute' : 'remoteAccess.optimizeRoute')}
      note={optimizeNote(t, optimize)}
      failed={Boolean(optimize?.problem)}
      busy={optimize?.busy}
      disabled={optimize?.disabled}
      onClick={onOptimize}
    />
  );
}
