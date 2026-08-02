import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText, Loader2, RefreshCw } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryItem,
  MemoryItemsResult,
  MemoryProfile,
  MemoryProfileReportResult,
} from '../../../context/ApiContext';
import { classifyMemoryResult, memoryErrorMessage } from '../../../lib/memoryRead';
import {
  acceptsProfileReportCompletion,
  profileReportLanguage,
  profileReportRequestKey,
} from './profileReportState';
import { useMemoryResource } from './useMemoryResource';

type MemoryItemsOk = Extract<MemoryItemsResult, { status: 'ok' }>;
type MemoryProfileReportOk = Extract<MemoryProfileReportResult, { status: 'ok' }>;
type Translate = (key: string) => string;

type ProfileReportViewState = {
  key: string | null;
  loading: boolean;
  report: string | null;
  warning: 'empty' | 'unstructured' | null;
  error: string | null;
};

const emptyProfileReportState = (key: string): ProfileReportViewState => ({
  key,
  loading: false,
  report: null,
  warning: null,
  error: null,
});

/** Return the first structured provider profile, leaving legacy raw items untouched. */
export const structuredProfileFromItems = (items: readonly MemoryItem[] | null): MemoryProfile | null =>
  items?.find((item) => item.kind === 'profile' && item.profile)?.profile ?? null;

const ProfileDetail: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-muted">
    <span className="font-medium text-foreground">{label}: </span>
    {value}
  </p>
);

/** Deterministic renderer for provider data. Every value remains an inert text node. */
export const StructuredMemoryProfile: React.FC<{ profile: MemoryProfile; t: Translate }> = ({ profile, t }) => (
  <div className="flex flex-col gap-4">
    {profile.summary ? (
      <section>
        <h3 className="text-[12px] font-semibold text-foreground">{t('memory.profile.summary')}</h3>
        <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{profile.summary}</p>
      </section>
    ) : null}
    {profile.explicit_info.length > 0 ? (
      <section>
        <h3 className="text-[12px] font-semibold text-foreground">{t('memory.profile.explicitInfo')}</h3>
        <div className="mt-2 flex flex-col gap-3">
          {profile.explicit_info.map((info, index) => (
            <div key={`${info.description}:${index}`} className="border-l-2 border-border pl-3">
              {info.category ? <Badge variant="secondary">{info.category}</Badge> : null}
              <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{info.description}</p>
              {info.evidence ? <ProfileDetail label={t('memory.profile.evidence')} value={info.evidence} /> : null}
            </div>
          ))}
        </div>
      </section>
    ) : null}
    {profile.implicit_traits.length > 0 ? (
      <section>
        <h3 className="text-[12px] font-semibold text-foreground">{t('memory.profile.implicitTraits')}</h3>
        <div className="mt-2 flex flex-col gap-3">
          {profile.implicit_traits.map((trait, index) => (
            <div key={`${trait.description}:${index}`} className="border-l-2 border-border pl-3">
              {trait.trait ? <Badge variant="secondary">{trait.trait}</Badge> : null}
              <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{trait.description}</p>
              {trait.basis ? <ProfileDetail label={t('memory.profile.basis')} value={trait.basis} /> : null}
              {trait.evidence ? <ProfileDetail label={t('memory.profile.evidence')} value={trait.evidence} /> : null}
            </div>
          ))}
        </div>
      </section>
    ) : null}
  </div>
);

export const ProfileReportAction: React.FC<{
  enabled: boolean;
  generating: boolean;
  onGenerate: () => void;
  t: Translate;
}> = ({ enabled, generating, onGenerate, t }) => (
  <Button variant="secondary" size="sm" onClick={onGenerate} disabled={!enabled || generating}>
    {generating ? <Loader2 className="size-3.5 animate-spin" /> : <FileText className="size-3.5" />}
    {generating ? t('memory.profile.generatingReport') : t('memory.profile.generateReport')}
  </Button>
);

export const ProfileReportOutput: React.FC<{
  report: string | null;
  warning: 'empty' | 'unstructured' | null;
  error: string | null;
  t: Translate;
}> = ({ report, warning, error, t }) => {
  if (error) {
    return <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>;
  }
  if (report) {
    return (
      <section className="border-t border-border pt-4">
        <h3 className="text-[12px] font-semibold text-foreground">{t('memory.profile.reportTitle')}</h3>
        <pre className="mt-2 whitespace-pre-wrap break-words font-sans text-[13px] leading-relaxed text-foreground">{report}</pre>
      </section>
    );
  }
  if (warning) {
    return <p className="text-[12px] text-muted">{t(`memory.profile.reportWarning.${warning}`)}</p>;
  }
  return null;
};

export const MemoryProfilePanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t, i18n } = useTranslation();
  const api = useApi();
  // Only a successful response is the benign Provider-A case: `profile_warning:'empty'`
  // (or simply zero items) renders as the graceful "not available"/empty copy. A closed
  // failure — sidecar down, provider outage, timeout, etc. — is a real error and the
  // resource surfaces it distinctly per its code.
  const { data, error, loading, reload, revision: profileRevision } = useMemoryResource<MemoryItemsOk>({
    read: api.getMemoryProfile,
    failureMessageKey: 'memory.profile.loadFailed',
    enabled,
    // Refresh is an explicit gesture: report the new attempt, not the old failure.
    clearErrorOnReload: true,
    resetDataOnError: true,
  });
  const reportLanguage = profileReportLanguage(i18n.language);
  const reportKey = profileReportRequestKey(profileRevision, reportLanguage);
  const reportKeyRef = useRef(reportKey);
  reportKeyRef.current = reportKey;
  const [reportState, setReportState] = useState<ProfileReportViewState>(() => emptyProfileReportState(reportKey));

  useEffect(() => {
    void reload();
  }, [reload]);

  // A successful deterministic refresh or a language switch immediately hides
  // the prior transient report. The request-key guard below discards any late
  // completion from that older snapshot.
  useEffect(() => {
    setReportState((current) => (current.key === reportKey ? current : emptyProfileReportState(reportKey)));
  }, [reportKey]);

  const items = data?.items ?? null;
  const structuredProfile = structuredProfileFromItems(items);
  const visibleReport = reportState.key === reportKey ? reportState : emptyProfileReportState(reportKey);

  const generateReport = useCallback(async () => {
    if (!structuredProfile) return;
    const requestKey = reportKey;
    setReportState({ key: requestKey, loading: true, report: null, warning: null, error: null });
    try {
      const outcome = classifyMemoryResult<MemoryProfileReportOk>(
        await api.generateMemoryProfileReport(reportLanguage),
      );
      if (!acceptsProfileReportCompletion(requestKey, reportKeyRef.current)) return;
      if (outcome.kind === 'ok') {
        setReportState({
          key: requestKey,
          loading: false,
          report: outcome.value.report,
          warning: outcome.value.report_warning ?? null,
          error: null,
        });
        return;
      }
      setReportState({
        key: requestKey,
        loading: false,
        report: null,
        warning: null,
        error: memoryErrorMessage(t, outcome.code),
      });
    } catch {
      if (!acceptsProfileReportCompletion(requestKey, reportKeyRef.current)) return;
      setReportState({
        key: requestKey,
        loading: false,
        report: null,
        warning: null,
        error: t('memory.profile.reportFailed'),
      });
    }
  }, [api, reportKey, reportLanguage, structuredProfile, t]);

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.profile.disabledHint')}</div>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-[12.5px] text-muted">{t('memory.profile.description')}</p>
        <div className="flex items-center gap-2">
          <ProfileReportAction
            enabled={Boolean(structuredProfile) && !loading}
            generating={visibleReport.loading}
            onGenerate={() => void generateReport()}
            t={t}
          />
          <Button variant="ghost" size="sm" onClick={() => void reload()} disabled={loading}>
            {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            {t('memory.profile.refresh')}
          </Button>
        </div>
      </div>
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : loading && items === null ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('memory.profile.loading')}
        </div>
      ) : data?.profile_warning === 'empty' ? (
        <div className="rounded-2xl border border-gold/30 bg-gold/[0.06] p-6 text-center text-[13px] text-foreground">
          {t('memory.profile.warningUnavailable')}
        </div>
      ) : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.profile.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, index) => (
            <div key={`${item.kind}:${item.date ?? ''}:${index}`} className="rounded-xl border border-border bg-surface px-4 py-3">
              <div className="mb-3 flex items-center gap-2">
                <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
              </div>
              {item.profile ? (
                <StructuredMemoryProfile profile={item.profile} t={t} />
              ) : (
                <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
              )}
            </div>
          ))}
          {structuredProfile ? (
            <ProfileReportOutput
              report={visibleReport.report}
              warning={visibleReport.warning}
              error={visibleReport.error}
              t={t}
            />
          ) : null}
          <p className="px-1 text-[11px] text-muted">{t('memory.profile.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};
