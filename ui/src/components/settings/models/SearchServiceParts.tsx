import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { LockKeyhole } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { ToggleSwitch } from '../SettingsPrimitives';
import { Field } from './dialogFields';
import { formatMonthDay, formatSmallUsd } from './format';
import { serviceSettings } from './searchServices';
import { sourceStatePresentation } from './sourceStatePresentation';
import { VendorGlyph } from './vendorGlyph';
import type { HostedExecutorCandidate, HostedSearchServiceVendor, HostedServiceListPrice, HostedToolPut, HostedWebSearchToolStatus, Source } from './types';

export const SearchVendorGlyph: React.FC<{ vendor: string }> = ({ vendor }) => (
  <span className="model-hub-accent-tile--cyan model-hub-accent-ink--cyan flex size-8 shrink-0 items-center justify-center rounded-lg">
    <VendorGlyph vendor={vendor} />
  </span>
);

export const SearchServicePrice: React.FC<{ price: HostedServiceListPrice }> = ({ price }) => {
  const { t } = useTranslation();
  return <span className="flex flex-col gap-0.5">
    <span className="font-semibold text-foreground">{t('settings.models.searchServices.price', { usd: formatSmallUsd(price.usd_per_1k) })}</span>
    <a href={price.url} target="_blank" rel="noopener noreferrer" className="model-hub-tool-cost-note underline underline-offset-2">{t('settings.models.searchServices.priceDate', { date: price.as_of })}</a>
  </span>;
};

/** Positive evidence is display-only. Disabled/blocked/cooling always wins over an old success. */
export const SearchCandidateStatus: React.FC<{
  candidate: HostedExecutorCandidate;
  tool: HostedWebSearchToolStatus;
  source?: Source;
}> = ({ candidate, tool, source }) => {
  const { t, i18n } = useTranslation();
  const state = candidate.kind === 'service' ? candidate.state : source?.state;
  const success = tool.status.verified.find((row) => row.source_id === candidate.source_id && row.model_id === candidate.model_id);
  let text: string | null = null;
  let tone = 'model-hub-status-mint';
  if (candidate.kind === 'service' && !candidate.settings.enabled) {
    text = t('settings.models.searchServices.unused');
    tone = 'model-hub-accent-pill--neutral';
  } else if (state && (state.status === 'needs_action' || state.status === 'error' || state.status === 'cooldown')) {
    const presentation = sourceStatePresentation(state, 'card', i18n.language);
    const reason = candidate.kind === 'service' && state.detail_key === 'models.source.needs_action.credential_revoked'
      ? t('settings.models.searchServices.keyInvalid') : presentation.key ? t(presentation.key, presentation.values) : '';
    text = t('settings.models.searchServices.skipped', { reason });
    tone = 'model-hub-status-gold';
  } else if (candidate.kind === 'service' && candidate.cooldown_until !== null) {
    const date = new Date(candidate.cooldown_until);
    const today = new Date();
    const clock = new Intl.DateTimeFormat(i18n.language, { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
    const time = date.toDateString() === today.toDateString() ? clock : `${formatMonthDay(candidate.cooldown_until)} ${clock}`;
    text = t('settings.models.searchServices.cooldown', { time });
    tone = 'model-hub-status-gold';
  } else if (success) {
    text = t('settings.models.tools.pair.ok', { date: formatMonthDay(success.at) });
  } else if (source?.verification_pending) {
    text = t('settings.models.sourceDetail.status.saved');
    tone = 'model-hub-accent-pill--neutral';
  }
  return text ? <span className={cn('model-hub-pill border', tone)}>{text}</span> : null;
};

export const SearchServiceNotes: React.FC<{ vendor?: HostedSearchServiceVendor }> = ({ vendor }) => {
  const { t } = useTranslation();
  return <div className="model-hub-tool-note flex flex-col gap-2">
    <p>{vendor ? t('settings.models.searchServices.queryVendor', { vendor: vendor.name }) : t('settings.models.searchServices.queryDestination')}</p>
    <p>{t('settings.models.searchServices.placement')}</p>
    <p className="flex items-start gap-2"><LockKeyhole className="mt-0.5 size-3.5 shrink-0" aria-hidden />{t('settings.models.searchServices.custody')}</p>
  </div>;
};

export const SearchVendorFacts: React.FC<{ vendor: HostedSearchServiceVendor }> = ({ vendor }) => {
  const { t } = useTranslation();
  return <>
    <p className="model-hub-tool-note">{t(`settings.models.searchServices.vendor.${vendor.id}`, { defaultValue: t(vendor.facts.tool_only ? 'settings.models.searchServices.noModels' : 'settings.models.searchServices.modelsAndSearch') })}</p>
    <span className="model-hub-tool-note flex flex-wrap gap-2">
      {vendor.free_tier !== null && <span className="model-hub-pill border border-border">{vendor.free_tier}</span>}
      <span className="model-hub-pill border border-border">{t('settings.models.searchServices.maxResults', { count: vendor.results_max })}</span>
      {vendor.id === 'deepseek' && <span className="model-hub-pill border border-border">{t('settings.models.searchServices.deepseekFacts')}</span>}
    </span>
  </>;
};

/** Only per-field patches leave this section; effective defaults are never echoed into saved overrides. */
export const SearchServiceSettings: React.FC<{
  source: Source;
  tool: HostedWebSearchToolStatus;
  vendor: HostedSearchServiceVendor;
  pending: boolean;
  onSave: (body: HostedToolPut) => void;
  onRetry: (sourceId: string) => void;
}> = ({ source, tool, vendor, pending, onSave, onRetry }) => {
  const { t } = useTranslation();
  const settings = serviceSettings(tool, vendor, source.id);
  const candidate = tool.executor_candidates.find((row) => row.source_id === source.id);
  return <section className="flex shrink-0 flex-col gap-4 border-b border-border px-5 py-4" aria-label={t('settings.models.searchServices.settings')}>
    <div className="flex items-center justify-between gap-3">
      <span className="font-semibold text-foreground">{t('settings.models.searchServices.use')}</span>
      <ToggleSwitch enabled={settings.enabled} disabled={pending} label={t('settings.models.searchServices.use')} onClick={() => onSave({ services: { [source.id]: { enabled: !settings.enabled } } })} />
    </div>
    {candidate && <div className="flex flex-wrap items-center gap-2">
      <SearchCandidateStatus candidate={candidate} tool={tool} source={source} />
      {candidate.kind === 'service' && candidate.cooldown_until !== null && <Button variant="outline" size="xs" disabled={pending} onClick={() => onRetry(source.id)}>{t('settings.models.searchServices.retry')}</Button>}
    </div>}
    <Field label={t('settings.models.searchServices.results')}>
      {(id) => <Select id={id} value={settings.results} disabled={pending} wrapperClassName="max-w-48" onChange={(event) => onSave({ services: { [source.id]: { results: Number(event.target.value) } } })}>
        {Array.from({ length: vendor.results_max }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{t('settings.models.searchServices.resultCount', { count })}</option>)}
      </Select>}
    </Field>
    <p className="model-hub-tool-note">{t('settings.models.searchServices.settingsNote')}</p>
    <SearchServicePrice price={vendor.price} />
  </section>;
};
