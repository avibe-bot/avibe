// Settings › Experimental: a list of experiments, each with its own page (ui/design:
// settings/source-filter/*). A row only opens its page and never carries the switch, so a tap
// cannot turn an experiment on by mistake. Source filter is the first experiment
// (`docs/plans/vibey-source-filter-hooks.md`): the owner's scripts around Vibey's web search and
// web fetch. The page shows the folder and each script's state; it never uploads, edits or runs
// code, and the details live in the guide, not on the page.
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, BookOpen, Check, ChevronRight, Copy, Filter, TriangleAlert } from 'lucide-react';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { copyTextToClipboard } from '@/lib/utils';
import { formatDayTime } from '../models/format';
import { SettingsPanel, ToggleSwitch } from '../SettingsPrimitives';
import { SettingsPageShell } from '../SettingsPageShell';
import {
  SOURCE_FILTER_HOOKS,
  sourceFilterApi,
  sourceFilterGuideUrl,
  type SourceFilterHook,
  type SourceFilterStatus,
} from './sourceFilterApi';

export const SOURCE_FILTER_PATH = '/settings/experimental/source-filter';

function useSourceFilter() {
  const [status, setStatus] = useState<SourceFilterStatus | null>(null);
  const [error, setError] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let live = true;
    sourceFilterApi.get().then(
      (next) => { if (live) { setStatus(next); setError(false); } },
      () => { if (live) setError(true); },
    );
    return () => { live = false; };
  }, []);
  const setEnabled = useCallback(async (enabled: boolean) => {
    setSaving(true);
    try {
      setStatus(await sourceFilterApi.setEnabled(enabled));
      setError(false);
    } catch {
      setError(true);
    } finally {
      setSaving(false);
    }
  }, []);
  return { status, error, saving, setEnabled };
}

export const SettingsExperimentalPage: React.FC = () => {
  const { t } = useTranslation();
  const { status } = useSourceFilter();
  return (
    <SettingsPageShell
      activeTab="experimental"
      title={t('settings.experimental.title')}
      subtitle={t('settings.experimental.subtitle')}
    >
      <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-elevation-0">
        <Link
          to={SOURCE_FILTER_PATH}
          data-experiment="source-filter"
          className="flex min-h-16 items-center gap-3 px-4 py-3.5 hover:bg-surface-wash md:px-5"
        >
          <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-edge bg-field">
            <Filter className="size-4 text-mint-ink" />
          </span>
          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[14px] font-semibold text-foreground">{t('settings.experimental.sourceFilter.title')}</span>
            <span className="text-[12px] leading-relaxed text-muted">{t('settings.experimental.sourceFilter.summary')}</span>
          </span>
          {status && (
            status.enabled
              ? <Badge variant="success" className="shrink-0">{t('settings.experimental.on')}</Badge>
              : <Badge variant="secondary" className="shrink-0">{t('settings.experimental.off')}</Badge>
          )}
          <ChevronRight className="size-4 shrink-0 text-muted" />
        </Link>
      </div>
    </SettingsPageShell>
  );
};

const HookRow: React.FC<{ file: string; hook: SourceFilterHook }> = ({ file, hook }) => {
  const { t, i18n } = useTranslation();
  const failed = hook.found && hook.last_failure !== null;
  return (
    <div
      data-hook={file}
      className={clsx('flex flex-col gap-1 border-b border-border px-5 py-3 last:border-b-0', failed && 'bg-destructive/[0.04]')}
    >
      <div className="flex items-center justify-between gap-3">
        <span className={clsx('truncate font-mono text-[13px] font-medium', hook.found ? 'text-foreground' : 'text-muted')}>{file}</span>
        {failed ? (
          <Badge variant="destructive"><TriangleAlert className="size-3" />{t('settings.experimental.sourceFilter.lastFailed')}</Badge>
        ) : hook.found ? (
          <Badge variant="success"><Check className="size-3" />{t('settings.experimental.sourceFilter.found')}</Badge>
        ) : (
          <Badge variant="secondary">{t('settings.experimental.sourceFilter.notFound')}</Badge>
        )}
      </div>
      {failed && hook.last_failure && (
        // The reason is the runtime's own wording; the page stays short and keeps it for a hover.
        <span className="text-[11.5px] leading-relaxed text-muted" title={hook.last_failure.reason}>
          {t('settings.experimental.sourceFilter.failure', { time: formatDayTime(hook.last_failure.at, i18n.language) })}
        </span>
      )}
    </div>
  );
};

export const SettingsSourceFilterPage: React.FC = () => {
  const { t, i18n } = useTranslation();
  const { status, error, saving, setEnabled } = useSourceFilter();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (status && await copyTextToClipboard(status.dir)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <div className="flex flex-col gap-4">
      <Link
        to="/settings/experimental"
        className="inline-flex w-fit items-center gap-1.5 text-[12px] font-medium text-muted hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {t('settings.experimental.title')}
      </Link>
      <SettingsPageShell
        activeTab="experimental"
        title={t('settings.experimental.sourceFilter.title')}
        subtitle={t('settings.experimental.sourceFilter.summary')}
      >
        <SettingsPanel
          variant="preference"
          title={t('settings.experimental.sourceFilter.use')}
          description={(
            <a
              href={sourceFilterGuideUrl(i18n.language)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium text-cyan-ink hover:underline"
            >
              <BookOpen className="size-3.5" />
              {t('settings.experimental.sourceFilter.guide')}
            </a>
          )}
          actions={(
            <ToggleSwitch
              enabled={status?.enabled ?? false}
              disabled={status === null || saving}
              onClick={() => { if (status) void setEnabled(!status.enabled); }}
              label={t('settings.experimental.sourceFilter.title')}
            />
          )}
        />
        {error && <p role="alert" className="text-[12px] text-destructive">{t('settings.experimental.loadFailed')}</p>}

        {status?.enabled && (
          <SettingsPanel
            title={t('settings.experimental.sourceFilter.scripts')}
            description={t('settings.experimental.sourceFilter.scriptsHint')}
          >
            <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-2.5">
              <span className="min-w-0 break-all font-mono text-[12px] text-foreground">{status.dir}</span>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t('settings.experimental.sourceFilter.copyFolder')}
                className="size-8 shrink-0 text-muted"
                onClick={() => void copy()}
              >
                {copied ? <Check className="size-3.5 text-mint-ink" /> : <Copy className="size-3.5" />}
              </Button>
            </div>
            {SOURCE_FILTER_HOOKS.map((name) => (
              <HookRow key={name} file={`${name}.py`} hook={status.hooks[name]} />
            ))}
          </SettingsPanel>
        )}
      </SettingsPageShell>
    </div>
  );
};
