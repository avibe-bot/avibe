import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, RefreshCw } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { useApi } from '../../../context/ApiContext';
import type { MemoryItem, MemoryItemsResult, MemoryProfile } from '../../../context/ApiContext';
import { useMemoryResource } from './useMemoryResource';
import { memoryOriginLabelKey } from './memoryOrigin';

type MemoryItemsOk = Extract<MemoryItemsResult, { status: 'ok' }>;
type Translate = (key: string) => string;

const ProfileDetail: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-muted">
    <span className="font-medium text-foreground">{label}: </span>
    {value}
  </p>
);

/** Render provider values as inert text nodes, never as Markdown or HTML. */
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

export const MemoryProfileItemBlock: React.FC<{ item: MemoryItem; t: Translate }> = ({ item, t }) => (
  <div className="rounded-xl border border-border bg-surface px-4 py-3">
    <div className="mb-3 flex items-center gap-2">
      <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
      {memoryOriginLabelKey(item.origin) ? (
        <Badge variant="outline">{t(memoryOriginLabelKey(item.origin)!)}</Badge>
      ) : null}
      {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
    </div>
    {item.profile ? (
      <StructuredMemoryProfile profile={item.profile} t={t} />
    ) : (
      <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
    )}
  </div>
);

export const MemoryProfilePanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  // Only a SUCCESSFUL response is the benign Provider-A case: `profile_warning:'empty'`
  // (or simply zero items) renders as the graceful empty-state copy. A closed
  // failure — sidecar down, provider outage, timeout, etc. — is a real ERROR, and the
  // resource surfaces it distinctly per its code.
  const { data, error, loading, reload } = useMemoryResource<MemoryItemsOk>({
    read: api.getMemoryProfile,
    failureMessageKey: 'memory.profile.loadFailed',
    enabled,
    // Refresh is an explicit gesture: report the new attempt, not the old failure.
    clearErrorOnReload: true,
    resetDataOnError: true,
  });

  useEffect(() => {
    void reload();
  }, [reload]);

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.profile.disabledHint')}</div>;
  }

  const items = data?.items ?? null;
  const partial = data?.warnings.includes('memory_search_partial') ?? false;
  const partialEmpty = partial && items !== null && items.length === 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12.5px] text-muted">{t('memory.profile.description')}</p>
        <Button variant="ghost" size="sm" onClick={() => void reload()} disabled={loading}>
          {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
          {t('memory.profile.refresh')}
        </Button>
      </div>
      {partial ? (
        <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm text-muted">
          {t('memory.profile.partial')}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{error}</div>
      ) : loading && items === null ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('memory.profile.loading')}
        </div>
      ) : partialEmpty ? null : data?.profile_warning === 'empty' ? (
        <div className="rounded-2xl border border-gold/30 bg-gold/[0.06] p-6 text-center text-[13px] text-foreground">
          {t('memory.profile.warningEmpty')}
        </div>
      ) : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.profile.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, index) => (
            <MemoryProfileItemBlock
              key={`${item.kind}:${item.date ?? ''}:${index}`}
              item={item}
              t={t}
            />
          ))}
          <p className="px-1 text-[11px] text-muted">{t('memory.profile.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};
