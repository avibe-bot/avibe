import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink, FileText, Loader2, RefreshCw } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryItem,
  MemoryItemsResult,
  MemoryProfile,
  MemoryProfilePageDescriptor,
  MemoryProfileReportResult,
} from '../../../context/ApiContext';
import { classifyMemoryResult, memoryErrorMessage } from '../../../lib/memoryRead';
import {
  acceptsProfilePageCompletion,
  profilePageFreshness,
  profileReportLanguage,
} from './profileReportState';
import { useMemoryResource } from './useMemoryResource';

type MemoryItemsOk = Extract<MemoryItemsResult, { status: 'ok' }>;
type MemoryProfileReportOk = Extract<MemoryProfileReportResult, { status: 'ok' }>;
type Translate = (key: string) => string;

type ProfilePageViewState = {
  language: 'en' | 'zh';
  loading: boolean;
  generating: boolean;
  page: MemoryProfilePageDescriptor | null;
  warning: 'empty' | 'unstructured' | null;
  error: string | null;
};

const emptyProfilePageState = (language: 'en' | 'zh', loading = false): ProfilePageViewState => ({
  language,
  loading,
  generating: false,
  page: null,
  warning: null,
  error: null,
});

/** Return the first structured provider profile, leaving legacy raw items untouched. */
export const structuredProfileFromItems = (items: readonly MemoryItem[] | null): MemoryProfile | null =>
  items?.find((item) => item.kind === 'profile' && item.profile)?.profile ?? null;

/** Open the private page without retaining an opener, while preserving same-origin auth evidence. */
export const openMemoryProfilePage = (viewUrl: string): void => {
  window.open(viewUrl, '_blank', 'noopener');
};

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
    {generating ? t('memory.profile.generatingPage') : t('memory.profile.generatePage')}
  </Button>
);

export const ProfilePageOutput: React.FC<{
  page: MemoryProfilePageDescriptor | null;
  freshness: 'current' | 'stale' | 'unknown';
  loading: boolean;
  generating: boolean;
  warning: 'empty' | 'unstructured' | null;
  error: string | null;
  onOpen: () => void;
  t: Translate;
}> = ({ page, freshness, loading, generating, warning, error, onOpen, t }) => (
  <section className="border-t border-border pt-4">
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h3 className="text-[12px] font-semibold text-foreground">{t('memory.profile.pageTitle')}</h3>
        {page ? (
          <Badge variant={freshness === 'current' ? 'success' : freshness === 'stale' ? 'warning' : 'secondary'}>
            {t(`memory.profile.pageFreshness.${freshness}`)}
          </Badge>
        ) : null}
        {generating ? <Badge variant="info">{t('memory.profile.generatingPage')}</Badge> : null}
      </div>
      {page ? (
        <Button
          variant="ghost"
          size="icon"
          onClick={onOpen}
          aria-label={t('memory.profile.openPage')}
          title={t('memory.profile.openPage')}
        >
          <ExternalLink className="size-4" />
        </Button>
      ) : null}
    </div>
    {error ? (
      <div className="mt-3 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        {error}
      </div>
    ) : null}
    {warning ? (
      <p className="mt-3 text-[12px] text-muted">{t(`memory.profile.reportWarning.${warning}`)}</p>
    ) : null}
    {page ? (
      <>
        <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-muted">
          <div className="flex min-w-0 gap-1.5">
            <dt>{t('memory.profile.generatedAt')}</dt>
            <dd className="break-all font-mono text-foreground">{page.generated_at}</dd>
          </div>
          {page.source_profile_updated_at ? (
            <div className="flex min-w-0 gap-1.5">
              <dt>{t('memory.profile.sourceUpdatedAt')}</dt>
              <dd className="break-all font-mono text-foreground">{page.source_profile_updated_at}</dd>
            </div>
          ) : null}
        </dl>
        <iframe
          key={page.artifact_id}
          src={page.view_url}
          title={t('memory.profile.pageTitle')}
          sandbox=""
          className="mt-3 h-[560px] min-h-[360px] w-full border border-border bg-white"
        />
      </>
    ) : loading || generating ? (
      <div className="mt-3 flex h-24 items-center justify-center gap-2 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('memory.profile.loadingPage')}
      </div>
    ) : (
      <p className="mt-3 text-[12px] text-muted">{t('memory.profile.noPage')}</p>
    )}
  </section>
);

export const MemoryProfilePanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t, i18n } = useTranslation();
  const api = useApi();
  // Only a successful response is the benign Provider-A case: `profile_warning:'empty'`
  // (or simply zero items) renders as the graceful "not available"/empty copy. A closed
  // failure — sidecar down, provider outage, timeout, etc. — is a real error and the
  // resource surfaces it distinctly per its code.
  const { data, error, loading, reload } = useMemoryResource<MemoryItemsOk>({
    read: api.getMemoryProfile,
    failureMessageKey: 'memory.profile.loadFailed',
    enabled,
    // Refresh is an explicit gesture: report the new attempt, not the old failure.
    clearErrorOnReload: true,
    resetDataOnError: true,
  });
  const reportLanguage = profileReportLanguage(i18n.language);
  const pageLanguageRef = useRef(reportLanguage);
  pageLanguageRef.current = reportLanguage;
  const [pageState, setPageState] = useState<ProfilePageViewState>(() =>
    emptyProfilePageState(reportLanguage, true),
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!enabled) {
      setPageState(emptyProfilePageState(reportLanguage));
      return;
    }
    let active = true;
    const requestedLanguage = reportLanguage;
    setPageState(emptyProfilePageState(requestedLanguage, true));
    void (async () => {
      try {
        const outcome = classifyMemoryResult<MemoryProfileReportOk>(
          await api.getMemoryProfilePage(requestedLanguage),
        );
        if (
          !active ||
          !acceptsProfilePageCompletion(requestedLanguage, pageLanguageRef.current)
        ) return;
        if (outcome.kind === 'ok') {
          setPageState({
            language: requestedLanguage,
            loading: false,
            generating: false,
            page: outcome.value.page,
            warning: outcome.value.report_warning ?? null,
            error: null,
          });
          return;
        }
        setPageState({
          ...emptyProfilePageState(requestedLanguage),
          error: memoryErrorMessage(t, outcome.code),
        });
      } catch {
        if (
          !active ||
          !acceptsProfilePageCompletion(requestedLanguage, pageLanguageRef.current)
        ) return;
        setPageState({
          ...emptyProfilePageState(requestedLanguage),
          error: t('memory.profile.pageLoadFailed'),
        });
      }
    })();
    return () => {
      active = false;
    };
  }, [api, enabled, reportLanguage, t]);

  const items = data?.items ?? null;
  const structuredProfile = structuredProfileFromItems(items);
  const visiblePage = pageState.language === reportLanguage
    ? pageState
    : emptyProfilePageState(reportLanguage, true);
  const freshness = profilePageFreshness(
    data?.profile_snapshot_id,
    visiblePage.page?.source_profile_snapshot_id,
  );

  const generateReport = useCallback(async () => {
    if (!structuredProfile) return;
    const requestedLanguage = reportLanguage;
    setPageState((current) => ({
      ...(current.language === requestedLanguage
        ? current
        : emptyProfilePageState(requestedLanguage)),
      generating: true,
      warning: null,
      error: null,
    }));
    try {
      const outcome = classifyMemoryResult<MemoryProfileReportOk>(
        await api.generateMemoryProfilePage(requestedLanguage),
      );
      if (!acceptsProfilePageCompletion(requestedLanguage, pageLanguageRef.current)) return;
      if (outcome.kind === 'ok') {
        setPageState((current) => ({
          ...(current.language === requestedLanguage
            ? current
            : emptyProfilePageState(requestedLanguage)),
          loading: false,
          generating: false,
          page:
            outcome.value.page ??
            (current.language === requestedLanguage ? current.page : null),
          warning: outcome.value.report_warning ?? null,
          error: null,
        }));
        return;
      }
      setPageState((current) => ({
        ...(current.language === requestedLanguage
          ? current
          : emptyProfilePageState(requestedLanguage)),
        generating: false,
        error: memoryErrorMessage(t, outcome.code),
      }));
    } catch {
      if (!acceptsProfilePageCompletion(requestedLanguage, pageLanguageRef.current)) return;
      setPageState((current) => ({
        ...(current.language === requestedLanguage
          ? current
          : emptyProfilePageState(requestedLanguage)),
        generating: false,
        error: t('memory.profile.pageGenerationFailed'),
      }));
    }
  }, [api, reportLanguage, structuredProfile, t]);

  const openPage = useCallback(() => {
    if (!visiblePage.page) return;
    openMemoryProfilePage(visiblePage.page.view_url);
  }, [visiblePage.page]);

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
            generating={visiblePage.generating}
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
        </div>
      )}
      <ProfilePageOutput
        page={visiblePage.page}
        freshness={freshness}
        loading={visiblePage.loading}
        generating={visiblePage.generating}
        warning={visiblePage.warning}
        error={visiblePage.error}
        onOpen={openPage}
        t={t}
      />
      {items && items.length > 0 ? (
        <p className="px-1 text-[11px] text-muted">{t('memory.profile.sourceNote')}</p>
      ) : null}
    </div>
  );
};
