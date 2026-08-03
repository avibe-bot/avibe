import { useEffect, useMemo, useState } from 'react';
import { Check, Compass, Github, Loader2, Plus, Search, Star, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../../context/ApiContext';
import type { SkillScope, SkillSearchItem } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { AiScoreBadge } from './QualityMeter';
import { errorMessage } from '@/lib/errorMessage';

export interface BrowseRegistryDialogProps {
  scope: SkillScope;
  projectId?: string;
  installedNames: Set<string>;
  onClose: () => void;
  onInstalled: () => void;
}

/** Search the askill.sh registry (askill find) and install results. */
export function BrowseRegistryDialog({ scope, projectId, installedNames, onClose, onInstalled }: BrowseRegistryDialogProps) {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SkillSearchItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [tag, setTag] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);

  // Debounced registry search; an empty query lists popular skills.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const handle = window.setTimeout(async () => {
      try {
        const res = await api.findSkills(query);
        if (!cancelled) setResults(res.ok && res.skills ? res.skills : []);
      } catch {
        if (!cancelled) setResults([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [api, query]);

  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const skill of results) for (const tg of skill.tags ?? []) counts.set(tg, (counts.get(tg) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([name]) => name);
  }, [results]);

  const shown = tag ? results.filter((s) => (s.tags ?? []).includes(tag)) : results;

  const install = async (item: SkillSearchItem) => {
    setInstalling(item.installSource);
    try {
      const res = await api.addSkill({ source: item.installSource, scope, projectId, all: false });
      if (res.ok) {
        showToast(t('skills.addSuccess', { count: 1 }), 'success');
        onInstalled();
      } else {
        showToast(res.error?.message ?? t('skills.addSkill'), 'error');
      }
    } catch (err) {
      showToast(errorMessage(err) ?? String(err), 'error');
    } finally {
      setInstalling(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[#05050B]/[0.72] p-10 backdrop-blur-[6px]">
      <div className="flex w-full max-w-[760px] flex-col overflow-hidden rounded-2xl border border-border-strong bg-surface-2 shadow-[0_24px_60px_-12px_rgba(0,0,0,0.7)]">
        <div className="flex items-center gap-3 border-b border-border px-5 py-4">
          <span className="flex size-[34px] shrink-0 items-center justify-center rounded-[9px] border border-cyan/40 bg-cyan-soft text-cyan">
            <Compass className="size-[17px]" />
          </span>
          <div className="flex flex-1 flex-col">
            <div className="text-[16px] font-bold text-foreground">{t('skills.browse.title')}</div>
            <div className="text-[11px] text-muted">{t('skills.browse.subtitle')}</div>
          </div>
          <button type="button" onClick={onClose} aria-label={t('common.close')} className="flex size-[26px] items-center justify-center rounded-md text-muted transition hover:bg-foreground/[0.06]">
            <X className="size-3.5" />
          </button>
        </div>

        <div className="flex flex-col gap-3.5 p-5">
          <div className="flex items-center gap-2.5 rounded-[10px] border border-border-strong bg-surface px-3.5 py-2.5">
            <Search className="size-[15px] text-muted" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('skills.browse.searchPlaceholder')}
              className="flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted"
            />
            {loading ? <Loader2 className="size-3.5 animate-spin text-muted" /> : (
              <span className="font-mono text-[10.5px] text-muted">{t('skills.browse.resultsCount', { count: results.length })}</span>
            )}
          </div>

          {tags.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                onClick={() => setTag(null)}
                className={clsx(
                  'rounded-full border px-2.5 py-1 text-[11.5px] transition',
                  tag === null ? 'border-mint/40 bg-mint-soft font-semibold text-mint' : 'border-border-strong text-muted hover:text-foreground',
                )}
              >
                {t('skills.browse.tagAll')}
              </button>
              {tags.map((tg) => (
                <button
                  key={tg}
                  type="button"
                  onClick={() => setTag(tg)}
                  className={clsx(
                    'rounded-full border px-2.5 py-1 text-[11.5px] transition',
                    tag === tg ? 'border-mint/40 bg-mint-soft font-semibold text-mint' : 'border-border-strong text-muted hover:text-foreground',
                  )}
                >
                  {tg}
                </button>
              ))}
            </div>
          ) : null}

          <div className="flex max-h-[420px] flex-col gap-2.5 overflow-y-auto">
            {shown.length === 0 && !loading ? (
              <div className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-[12px] text-muted">
                {t('skills.browse.empty')}
              </div>
            ) : null}
            {shown.map((item) => {
              const installed = installedNames.has(item.name);
              const isInstalling = installing === item.installSource;
              return (
                <div key={String(item.id)} className="flex items-center gap-3 rounded-xl border border-border bg-surface px-3.5 py-3.5">
                  <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[14px] font-semibold text-foreground">{item.name}</span>
                      <AiScoreBadge score={item.aiScore} />
                      <Github className="size-3 text-muted" />
                      <span className="font-mono text-[10px] text-muted">
                        {item.owner}
                        {item.repo ? `/${item.repo}` : ''}
                      </span>
                    </div>
                    <div className="line-clamp-2 text-[11.5px] text-muted">{item.description}</div>
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-muted">
                      {(item.tags ?? []).slice(0, 3).map((tg) => (
                        <span key={tg} className="rounded border border-border bg-surface-3 px-1.5 py-0.5">
                          {tg}
                        </span>
                      ))}
                      {typeof item.stars === 'number' ? (
                        <span className="flex items-center gap-1">
                          <Star className="size-3 text-gold" />
                          {item.stars}
                        </span>
                      ) : null}
                    </div>
                  </div>
                  {installed ? (
                    <span className="flex shrink-0 items-center gap-1.5 rounded-lg border border-border-strong bg-surface-3 px-3 py-2 text-[12px] font-medium text-muted">
                      <Check className="size-3.5 text-mint" />
                      {t('skills.browse.installed')}
                    </span>
                  ) : (
                    <button
                      type="button"
                      // Any install in flight disables all Add buttons — askill
                      // installs share one lock file and races drop entries.
                      disabled={installing !== null}
                      onClick={() => install(item)}
                      className="flex shrink-0 items-center gap-1.5 rounded-lg border border-mint/40 bg-mint-soft px-3.5 py-2 text-[12px] font-semibold text-mint transition hover:brightness-110 disabled:opacity-60"
                    >
                      {isInstalling ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
                      {t('skills.browse.add')}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-border bg-surface-3 px-5 py-3.5">
          <span className="font-mono text-[10.5px] text-muted">
            {t('skills.browse.showing', { shown: shown.length, total: results.length })}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border-strong px-4 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
          >
            {t('skills.browse.done')}
          </button>
        </div>
      </div>
    </div>
  );
}
