import { ArrowRight, Check, HeartPulse, Smartphone } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';
import { Badge, type BadgeProps } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { HealthRequest, HealthViewState } from '@/lib/healthRequests';
import { useHealthRequest } from './useHealthRequest';

const variants: Record<HealthViewState, BadgeProps['variant']> = {
  authorize: 'secondary', waiting: 'secondary', claimed: 'info', succeeded: 'success',
  connected: 'success', declined: 'secondary', failed: 'warning', expired: 'secondary',
  canceled: 'secondary', needsUpdate: 'warning',
};
type ViewProps = {
  state: HealthViewState;
  operation?: HealthRequest['operation'];
  metrics?: string[];
  busy?: boolean;
  busyPhase?: 'pairing' | 'native' | 'unresponsive';
  retryable?: boolean;
  readOnly?: boolean;
  loadError?: boolean;
  onAuthorize?: () => void;
};

/** ui/design: health/request-card — the approved card, driven by request metadata only. */
export function HealthRequestCardView({ state, operation = 'health.connect', metrics = ['steps', 'activity', 'workouts', 'sleep'], busy, busyPhase, retryable, readOnly, loadError, onAuthorize }: ViewProps) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-edge bg-surface px-3.5 py-3 shadow-elevation-0">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-pink/15 text-pink-ink">
        <HeartPulse className="size-4" />
      </div>
      <div className="flex min-w-0 flex-1 basis-(--health-card-copy-basis) flex-col gap-0.5" aria-live="polite">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted">{t(operation === 'health.connect' ? 'health.title.connect' : 'health.title.refresh')}</span>
          {state !== 'authorize' && <Badge variant={variants[state]}>{t(`health.status.${state}`)}</Badge>}
        </div>
        <span className="text-health-metric font-semibold text-foreground">{metrics.map(metric => t(`health.metric.${metric}`, { defaultValue: metric })).join(t('health.metricSeparator'))}</span>
        <span className="text-xs leading-5 text-muted">{t(state === 'waiting' && operation === 'health.refresh' ? 'health.waitingRefresh' : `health.note.${state}`)}</span>
        {loadError && <span role="status" className="text-xs leading-5 text-muted">{t('health.loadError')}</span>}
        {busyPhase === 'unresponsive' && <span role="status" className="text-xs leading-5 text-muted">{t('health.reopenApp')}</span>}
      </div>
      {(state === 'authorize' || retryable) && <Button size="sm" className="shrink-0 max-sm:w-full" disabled={busy || loadError || readOnly} onClick={onAuthorize}>
        {t(busy ? (busyPhase === 'pairing' ? 'health.preparing' : 'health.connecting') : retryable ? 'health.retry' : 'health.authorize')}<ArrowRight className="size-3.5" />
      </Button>}
    </div>
  );
}

export function HealthRequestCard({ request, readOnly }: { request: HealthRequest; readOnly?: boolean }) {
  const live = useHealthRequest(request.id, request);
  return <HealthRequestCardView state={live.state ?? 'waiting'} operation={request.operation}
    readOnly={readOnly || !live.canAuthorize} metrics={live.request?.metrics ?? request.metrics} busy={live.busy} busyPhase={live.busyPhase} retryable={live.retryable} loadError={live.loadError} onAuthorize={readOnly ? undefined : live.authorize} />;
}

/** ui/design: health/landing — a notification trampoline; no health values or settings. */
export function HealthLandingView({ state, operation = 'health.connect', metrics = ['steps', 'activity', 'workouts', 'sleep'], busy, busyPhase, retryable, readOnly, loadError, onAuthorize }: ViewProps) {
  const { t } = useTranslation();
  const connected = state === 'connected' || state === 'succeeded';
  const authorize = state === 'authorize';
  return (
    <div className="mx-auto flex min-h-(--health-landing-min-h) w-full max-w-(--health-landing-max-w) flex-col items-center justify-center px-5 py-10 text-center">
      <div className={cn('flex size-14 items-center justify-center rounded-2xl', connected ? 'bg-mint-soft text-mint-ink' : state === 'waiting' ? 'bg-cyan-soft text-cyan-ink' : 'bg-pink/15 text-pink-ink')}>
        {connected ? <Check className="size-7" /> : state === 'waiting' ? <Smartphone className="size-7" /> : <HeartPulse className="size-7" />}
      </div>
      <h1 className="mt-5 text-xl font-semibold leading-tight text-foreground">{t(authorize ? 'health.title.connect' : `health.status.${state}`)}</h1>
      <p className="mt-3 text-sm leading-6 text-muted" aria-live="polite">{t(authorize ? 'health.landing.authorize' : state === 'waiting' && operation === 'health.refresh' ? 'health.waitingRefresh' : `health.note.${state}`,
        { metrics: metrics.map(metric => t(`health.metric.${metric}`, { defaultValue: metric })).join(t('health.metricSeparator')) })}</p>
      {loadError && <p role="status" className="mt-3 text-sm text-muted">{t('health.loadError')}</p>}
      {busyPhase === 'unresponsive' && <p role="status" className="mt-3 text-sm text-muted">{t('health.reopenApp')}</p>}
      <div className="mt-7 flex w-full flex-col gap-2.5">
        {(authorize || retryable) && operation === 'health.connect' && <Button variant="brand" size="lg" className="w-full" disabled={busy || loadError || readOnly} onClick={onAuthorize}>{t(busy ? (busyPhase === 'pairing' ? 'health.preparing' : 'health.connecting') : retryable ? 'health.retry' : 'health.authorize')}</Button>}
        <Button asChild variant={authorize ? 'ghost' : 'outline'} size="lg" className="w-full"><Link to="/inbox">{t('health.landing.openConversations')}</Link></Button>
      </div>
      <p className="mt-5 text-xs leading-5 text-muted">{t('health.readOnly')}</p>
    </div>
  );
}

function HealthLandingRequest({ id }: { id: string }) {
  const live = useHealthRequest(id);
  const { t } = useTranslation();
  if (!live.request || !live.state) return <div className="mx-auto flex min-h-(--health-landing-min-h) max-w-(--health-landing-max-w) flex-col justify-center gap-5 px-5 text-center">
    <p role="status">{t(live.loadError ? 'health.loadError' : 'health.loading')}</p>
    <Button asChild variant="outline"><Link to="/inbox">{t('health.landing.openConversations')}</Link></Button>
  </div>;
  return <HealthLandingView {...live} readOnly={!live.canAuthorize} state={live.state} operation={live.request.operation} metrics={live.request.metrics} onAuthorize={live.authorize} />;
}

export default function HealthLandingPage() {
  const { requestId = '' } = useParams();
  return <HealthLandingRequest key={requestId} id={requestId} />;
}
