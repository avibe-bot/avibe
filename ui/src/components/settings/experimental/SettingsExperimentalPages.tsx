// Settings › Experimental: a list of experiments, each with its own page (ui/design:
// settings/source-filter/*). A row only opens its page and never carries the switch, so a tap
// cannot turn an experiment on by mistake. Source filter is the first experiment
// (`docs/plans/vibey-source-filter-hooks.md`): the owner's tool hooks. Script names open the
// existing Editor app; this page never uploads, creates or runs code. Details live in the guide.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, BookOpen, Check, ChevronRight, Copy, Filter, TriangleAlert } from 'lucide-react';
import clsx from 'clsx';

import { useAppLaunch } from '@/apps/useAppLaunch';
import { FilesApiError, fileBrowserErrorMessage, fileMeta, joinPath } from '@/lib/filesApi';
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
      title={t('settings.sections.experimental')}
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
              ? <Badge variant="success" className="shrink-0">{t('devices.capability.stateOn')}</Badge>
              : <Badge variant="secondary" className="shrink-0">{t('settings.general.appLock.off')}</Badge>
          )}
          <ChevronRight className="size-4 shrink-0 text-muted" />
        </Link>
      </div>
    </SettingsPageShell>
  );
};

const HookRow: React.FC<{ file: string; hook: SourceFilterHook; onOpen: () => void }> = ({ file, hook, onOpen }) => {
  const { t, i18n } = useTranslation();
  const failed = hook.found && hook.last_failure !== null;
  return (
    <div
      data-hook={file}
      className={clsx('flex flex-col gap-1 border-b border-border px-5 py-3 last:border-b-0', failed && 'bg-destructive/[0.04]')}
    >
      <div className="flex items-center justify-between gap-3">
        <Button
          type="button"
          variant="link"
          className="h-auto min-w-0 justify-start p-0 font-mono text-[13px] font-medium"
          onClick={onOpen}
        >
          <span className="truncate">{file}</span>
        </Button>
        {failed ? (
          <Badge variant="destructive"><TriangleAlert className="size-3" />{t('settings.experimental.sourceFilter.lastFailed')}</Badge>
        ) : hook.found ? (
          <Badge variant="success"><Check className="size-3" />{t('common.found')}</Badge>
        ) : (
          <Badge variant="secondary">{t('common.notFound')}</Badge>
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
  const [openError, setOpenError] = useState<string | null>(null);
  const launchApp = useAppLaunch('deep-link');
  const openRequest = useRef(0);
  useEffect(() => () => { openRequest.current += 1; }, [status?.dir, status?.enabled]);
  const openScript = async (file: string) => {
    if (!status?.enabled) return;
    const request = ++openRequest.current;
    const path = joinPath(status.dir, file);
    setOpenError(null);
    try {
      // Carry the save-conflict baseline, just like opening a file from the File Browser.
      let mtime: number | null = null;
      try {
        mtime = (await fileMeta(path)).mtime;
      } catch (cause) {
        // Missing scripts stay clickable, but opening one must not create an executable hook.
        // Let the existing Editor display its ordinary file-not-found state.
        if (!(cause instanceof FilesApiError) || cause.code !== 'not_found') throw cause;
      }
      if (request !== openRequest.current) return;
      await launchApp({ appId: 'editor', title: file, params: { path, filename: file, rootDir: status.dir, mtime } });
    } catch (cause) {
      if (request === openRequest.current) {
        setOpenError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.loadFailed')));
      }
    }
  };
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
        {t('settings.sections.experimental')}
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
        {openError && <p role="alert" className="text-[12px] text-destructive">{openError}</p>}

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
              <HookRow key={name} file={`${name}.py`} hook={status.hooks[name]} onOpen={() => void openScript(`${name}.py`)} />
            ))}
          </SettingsPanel>
        )}
      </SettingsPageShell>
    </div>
  );
};
