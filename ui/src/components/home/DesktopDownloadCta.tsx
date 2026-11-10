import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, MonitorDown } from 'lucide-react';

import {
  desktopDownloadUrl,
  detectDesktopDownloadSelection,
  type DesktopDownloadPlatform,
  type MacosArchitecture,
} from '@/lib/desktopDownload';
import { Button } from '../ui/button';
import { Card } from '../ui/card';
import { Select } from '../ui/select';
import { SegmentedRadio } from '../ui/segmented';

const PLATFORM_OPTIONS: DesktopDownloadPlatform[] = ['unknown', 'macos', 'windows'];

export function DesktopDownloadCta() {
  const { t } = useTranslation();
  const [selection, setSelection] = useState(detectDesktopDownloadSelection);
  const target = desktopDownloadUrl(selection);
  const macos = selection.platform === 'macos';

  return (
    <Card aria-labelledby="desktop-download-title" className="w-full">
      <div className="flex flex-col gap-4 p-4 sm:p-5">
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-cyan-soft text-cyan-ink">
            <MonitorDown className="size-4" aria-hidden="true" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 id="desktop-download-title" className="text-[15px] font-semibold text-foreground">
              {t('workbench.home.desktopDownload.title')}
            </h2>
            <p className="mt-1 text-[13px] leading-5 text-muted">
              {t('workbench.home.desktopDownload.description')}
            </p>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <label className="flex min-w-0 flex-col gap-1.5 text-xs font-medium text-foreground">
            {t('workbench.home.desktopDownload.platformLabel')}
            <Select
              aria-label={t('workbench.home.desktopDownload.platformLabel')}
              value={selection.platform}
              onChange={(event) =>
                setSelection((current) => ({
                  ...current,
                  platform: event.target.value as DesktopDownloadPlatform,
                }))}
            >
              {PLATFORM_OPTIONS.map((platform) => (
                <option key={platform} value={platform}>
                  {t(`workbench.home.desktopDownload.platforms.${platform}`)}
                </option>
              ))}
            </Select>
          </label>

          {macos ? (
            <div className="flex min-w-0 flex-col gap-1.5 text-xs font-medium text-foreground">
              {t('workbench.home.desktopDownload.architectureLabel')}
              <SegmentedRadio
                value={selection.macosArchitecture}
                onChange={(macosArchitecture: MacosArchitecture) =>
                  setSelection((current) => ({ ...current, macosArchitecture }))}
                ariaLabel={t('workbench.home.desktopDownload.architectureLabel')}
                options={[
                  {
                    id: 'arm64',
                    label: t('workbench.home.desktopDownload.architectures.arm64'),
                  },
                  {
                    id: 'x64',
                    label: t('workbench.home.desktopDownload.architectures.x64'),
                  },
                ]}
                className="w-full"
              />
            </div>
          ) : (
            <div className="flex items-end text-xs leading-5 text-muted">
              {selection.platform === 'windows'
                ? t('workbench.home.desktopDownload.windowsDetail')
                : t('workbench.home.desktopDownload.unknownDetail')}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-border pt-3 sm:flex-row sm:items-center sm:justify-between">
          <p role="status" className="text-xs leading-5 text-muted">
            {target
              ? t('workbench.home.desktopDownload.ready', {
                  format: selection.platform === 'windows' ? 'EXE' : 'DMG',
                })
              : t('workbench.home.desktopDownload.choosePlatform')}
          </p>
          {target ? (
            <Button asChild variant="brand-cyan" size="sm" className="shrink-0">
              <a href={target}>
                <Download aria-hidden="true" />
                {t('workbench.home.desktopDownload.action')}
              </a>
            </Button>
          ) : (
            <Button type="button" variant="brand-cyan" size="sm" className="shrink-0" disabled>
              <Download aria-hidden="true" />
              {t('workbench.home.desktopDownload.action')}
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
